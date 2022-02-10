"""Model for DeformingPlate."""

import torch
from torch import nn as nn
import torch.nn.functional as F

import common
import normalization
import encode_process_decode

import torch_scatter

device = torch.device('cuda')


class Model(nn.Module):
    """Model for static cloth simulation."""

    def __init__(self, params, core_model_name=encode_process_decode, message_passing_aggregator='sum',
                 message_passing_steps=15, attention=False, ripple_used=False, ripple_generation=None,
                 ripple_generation_number=None,
                 ripple_node_selection=None, ripple_node_selection_random_top_n=None, ripple_node_connection=None,
                 ripple_node_ncross=None):
        super(Model, self).__init__()
        self._params = params
        self._output_normalizer = normalization.Normalizer(size=3, name='output_normalizer')
        self._node_normalizer = normalization.Normalizer(size=3, name='node_normalizer')
        self._node_dynamic_normalizer = normalization.Normalizer(size=1, name='node_normalizer')
        self._mesh_edge_normalizer = normalization.Normalizer(size=8, name='mesh_edge_normalizer')
        self._world_edge_normalizer = normalization.Normalizer(size=4, name='world_edge_normalizer')
        self._model_type = params['model'].__name__
        self._displacement_base = None

        self.core_model_name = core_model_name
        self.core_model = encode_process_decode
        self.message_passing_steps = message_passing_steps
        self.message_passing_aggregator = message_passing_aggregator
        self._attention = attention
        self._ripple_used = ripple_used

        if self._ripple_used:
            self._ripple_generation = ripple_generation
            self._ripple_generation_number = ripple_generation_number
            self._ripple_node_selection = ripple_node_selection
            self._ripple_node_selection_random_top_n = ripple_node_selection_random_top_n
            self._ripple_node_connection = ripple_node_connection
            self._ripple_node_ncross = ripple_node_ncross
        if self._ripple_used:
            self.learned_model = self.core_model.EncodeProcessDecode(
                output_size=params['size'],
                latent_size=128,
                num_layers=2,
                message_passing_steps=self.message_passing_steps,
                message_passing_aggregator=self.message_passing_aggregator, attention=self._attention,
                ripple_used=self._ripple_used,
                ripple_generation=self._ripple_generation, ripple_generation_number=self._ripple_generation_number,
                ripple_node_selection=self._ripple_node_selection,
                ripple_node_selection_random_top_n=self._ripple_node_selection_random_top_n,
                ripple_node_connection=self._ripple_node_connection,
                ripple_node_ncross=self._ripple_node_ncross)
        else:
            self.learned_model = self.core_model.EncodeProcessDecode(
                output_size=params['size'],
                latent_size=128,
                num_layers=2,
                message_passing_steps=self.message_passing_steps,
                message_passing_aggregator=self.message_passing_aggregator, attention=self._attention,
                ripple_used=self._ripple_used)

    def unsorted_segment_operation(self, data, segment_ids, num_segments, operation):
        """
        Computes the sum along segments of a tensor. Analogous to tf.unsorted_segment_sum.

        :param data: A tensor whose segments are to be summed.
        :param segment_ids: The segment indices tensor.
        :param num_segments: The number of segments.
        :return: A tensor of same data type as the data argument.
        """
        assert all([i in data.shape for i in segment_ids.shape]), "segment_ids.shape should be a prefix of data.shape"

        # segment_ids is a 1-D tensor repeat it to have the same shape as data
        if len(segment_ids.shape) == 1:
            s = torch.prod(torch.tensor(data.shape[1:])).long().to(device)
            segment_ids = segment_ids.repeat_interleave(s).view(segment_ids.shape[0], *data.shape[1:]).to(device)

        assert data.shape == segment_ids.shape, "data.shape and segment_ids.shape should be equal"

        shape = [num_segments] + list(data.shape[1:])
        result = torch.zeros(*shape)
        if operation == 'sum':
            result = torch_scatter.scatter_add(data.float(), segment_ids, dim=0, dim_size=num_segments)
        elif operation == 'max':
            result, _ = torch_scatter.scatter_max(data.float(), segment_ids, dim=0, dim_size=num_segments)
        elif operation == 'mean':
            result = torch_scatter.scatter_mean(data.float(), segment_ids, dim=0, dim_size=num_segments)
        elif operation == 'min':
            result, _ = torch_scatter.scatter_min(data.float(), segment_ids, dim=0, dim_size=num_segments)
        else:
            raise Exception('Invalid operation type!')
        result = result.type(data.dtype)
        return result

    def _build_graph(self, inputs, is_training):
        """Builds input graph."""
        world_pos = inputs['world_pos']
        target_world_pos = inputs['target|world_pos']

        node_type = inputs['node_type']

        one_hot_node_type = F.one_hot(node_type[:, 0].to(torch.int64), common.NodeType.SIZE).float()

        cells = inputs['cells']
        decomposed_cells = common.triangles_to_edges(cells, deform=True)
        senders, receivers = decomposed_cells['two_way_connectivity']

        mesh_pos = inputs['mesh_pos']
        relative_mesh_pos = (torch.index_select(mesh_pos, 0, senders) -
                             torch.index_select(mesh_pos, 0, receivers))

        # find world edge
        radius = 0.006
        world_distance_matrix = torch.cdist(world_pos, world_pos, p=2)
        world_connection_matrix = torch.where(world_distance_matrix < radius, 1., 0.)
        # remove self connection
        world_connection_matrix = world_connection_matrix.fill_diagonal_(0.)
        # remove world edge node pairs that already exist in mesh edge collection
        world_connection_matrix[senders, receivers] = torch.tensor(0., dtype=torch.float32, device=device)
        # remove receivers whose node type is obstacle or handle
        no_connection_mask = torch.eq(node_type[:, 0], torch.tensor([common.NodeType.OBSTACLE.value], device=device))
        no_connection_mask = torch.logical_or(no_connection_mask, torch.eq(node_type[:, 0], torch.tensor([common.NodeType.HANDLE.value], device=device)))
        no_connection_mask = torch.transpose(torch.stack([no_connection_mask] * world_pos.shape[0], dim=1), 0, 1)
        world_connection_matrix = torch.where(no_connection_mask, torch.tensor(0., dtype=torch.float32, device=device), world_connection_matrix)

        world_senders, world_receivers = torch.nonzero(world_connection_matrix, as_tuple=True)
        relative_world_pos = (torch.index_select(input=world_pos, dim=0, index=world_senders) -
                              torch.index_select(input=world_pos, dim=0, index=world_receivers))

        world_edge_features = torch.cat((
            relative_world_pos,
            torch.norm(relative_world_pos, dim=-1, keepdim=True)), dim=-1)

        world_edges = self.core_model.EdgeSet(
            name='world_edges',
            features=self._world_edge_normalizer(world_edge_features, None, is_training),
            receivers=world_receivers,
            senders=world_senders)

        all_relative_world_pos = (torch.index_select(input=world_pos, dim=0, index=senders) -
                              torch.index_select(input=world_pos, dim=0, index=receivers))
        mesh_edge_features = torch.cat((
            relative_mesh_pos,
            torch.norm(relative_mesh_pos, dim=-1, keepdim=True),
            all_relative_world_pos,
            torch.norm(all_relative_world_pos, dim=-1, keepdim=True)), dim=-1)

        mesh_edges = self.core_model.EdgeSet(
            name='mesh_edges',
            features=self._mesh_edge_normalizer(mesh_edge_features, None, is_training),
            receivers=receivers,
            senders=senders)

        obstacle_mask = torch.eq(node_type[:, 0], torch.tensor([common.NodeType.OBSTACLE.value], device=device))
        obstacle_mask = torch.stack([obstacle_mask] * 3, dim=1)
        masked_target_world_pos = torch.where(obstacle_mask, target_world_pos, torch.tensor(0., dtype=torch.float32, device=device))
        masked_world_pos = torch.where(obstacle_mask, world_pos, torch.tensor(0., dtype=torch.float32, device=device))
        kinematic_nodes_features = self._node_normalizer(masked_target_world_pos - masked_world_pos)
        normal_node_features = torch.cat((torch.zeros_like(world_pos), one_hot_node_type), dim=-1)
        kinematic_node_features = torch.cat((kinematic_nodes_features, one_hot_node_type), dim=-1)
        obstacle_mask = torch.eq(node_type[:, 0], torch.tensor([common.NodeType.OBSTACLE.value], device=device))
        obstacle_mask = torch.stack([obstacle_mask] * 12, dim=1)
        node_features = torch.where(obstacle_mask, kinematic_node_features, normal_node_features)

        if self._ripple_used:
            num_nodes = node_type.shape[0]
            max_node_dynamic = self.unsorted_segment_operation(torch.norm(all_relative_world_pos, dim=-1), receivers, num_nodes,
                                                                operation='max').to(device)
            min_node_dynamic = self.unsorted_segment_operation(torch.norm(all_relative_world_pos, dim=-1), receivers,
                                                               num_nodes,
                                                               operation='min').to(device)
            node_dynamic = self._node_dynamic_normalizer(max_node_dynamic - min_node_dynamic)

            return (self.core_model.MultiGraphWithPos(node_features=node_features,
                                                     edge_sets=[mesh_edges, world_edges], target_feature=world_pos,
                                                     mesh_pos=mesh_pos, model_type=self._model_type, node_dynamic=node_dynamic))
        else:
            return (self.core_model.MultiGraph(node_features=node_features,
                                              edge_sets=[mesh_edges, world_edges]))

    def forward(self, inputs, is_training):
        graph = self._build_graph(inputs, is_training=is_training)
        if is_training:
            return self.learned_model(graph, self._mesh_edge_normalizer, world_edge_normalizer=self._world_edge_normalizer, is_training=is_training)
        else:
            return self._update(inputs, self.learned_model(graph, self._mesh_edge_normalizer, world_edge_normalizer=self._world_edge_normalizer, is_training=is_training))

    def _update(self, inputs, per_node_network_output):
        """Integrate model outputs."""
        velocity = self._output_normalizer.inverse(per_node_network_output)


        # integrate forward
        cur_position = inputs['world_pos']
        position = cur_position + velocity
        return (position, cur_position, velocity)

    def get_output_normalizer(self):
        return self._output_normalizer

    def save_model(self, path):
        torch.save(self.learned_model, path + "_learned_model.pth")
        torch.save(self._output_normalizer, path + "_output_normalizer.pth")
        torch.save(self._mesh_edge_normalizer, path + "_mesh_edge_normalizer.pth")
        torch.save(self._world_edge_normalizer, path + "_world_edge_normalizer.pth")
        torch.save(self._node_normalizer, path + "_node_normalizer.pth")

    def load_model(self, path):
        self.learned_model = torch.load(path + "_learned_model.pth")
        self._output_normalizer = torch.load(path + "_output_normalizer.pth")
        self._mesh_edge_normalizer = torch.load(path + "_mesh_edge_normalizer.pth")
        self._world_edge_normalizer = torch.load(path + "_world_edge_normalizer.pth")
        self._node_normalizer = torch.load(path + "_node_normalizer.pth")

    def evaluate(self):
        self.eval()
        self.learned_model.eval()
