# Lint as: python3
# pylint: disable=g-bad-file-header
# Copyright 2020 DeepMind Technologies Limited. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or  implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Model for FlagSimple."""

import torch
from torch import nn as nn
import torch.nn.functional as F

import common
import normalization
import encode_process_decode
import encode_process_decode_max_pooling
import encode_process_decode_lstm
import encode_process_decode_graph_structure_watcher
import encode_process_decode_hub

device = torch.device('cuda')

class Model(nn.Module):
    """Model for static cloth simulation."""

    def __init__(self, params, core_model_name, message_passing_aggregator, message_passing_steps):
        super(Model, self).__init__()
        self._params = params
        self._output_normalizer = normalization.Normalizer(size=3, name='output_normalizer')
        self._node_normalizer = normalization.Normalizer(
            size=3 + common.NodeType.SIZE, name='node_normalizer')
        self._edge_normalizer = normalization.Normalizer(
            size=7, name='edge_normalizer')  # 2D coord + 3D coord + 2*length = 7


        self.core_model_name = core_model_name
        self.core_model = self.select_core_model(core_model_name)
        self.message_passing_steps = message_passing_steps
        self.message_passing_aggregator = message_passing_aggregator
        self.learned_model = self.core_model.EncodeProcessDecode(
            output_size=params['size'],
            latent_size=128,
            num_layers=2,
            message_passing_steps=self.message_passing_steps,
            message_passing_aggregator=self.message_passing_aggregator)

    def select_core_model(self, core_model_name):
        return {
            'encode_process_decode': encode_process_decode,
            'encode_process_decode_graph_structure_watcher': encode_process_decode_graph_structure_watcher,
            'encode_process_decode_hub': encode_process_decode_hub,
            'encode_process_decode_max_pooling': encode_process_decode_max_pooling,
            'encode_process_decode_lstm': encode_process_decode_lstm,
        }.get(core_model_name, encode_process_decode)

    def get_core_model_name(self):
        return str(self.core_model)

    def get_message_passing_steps(self):
        return str(self.message_passing_steps)

    def get_message_passing_aggregator(self):
        return str(self.message_passing_aggregator)

    def _build_graph(self, inputs, is_training):
        """Builds input graph."""
        world_pos = inputs['world_pos']
        prev_world_pos = inputs['prev|world_pos']
        node_type = inputs['node_type']
        velocity = world_pos - prev_world_pos
        node_type = F.one_hot(node_type[:, 0].to(torch.int64), common.NodeType.SIZE)

        node_features = torch.cat((velocity, node_type), dim=-1)

        cells = inputs['cells']
        senders, receivers = common.triangles_to_edges(cells)

        mesh_pos = inputs['mesh_pos']
        relative_world_pos = (torch.index_select(input=world_pos, dim=0, index=senders) -
                              torch.index_select(input=world_pos, dim=0, index=receivers))
        relative_mesh_pos = (torch.index_select(mesh_pos, 0, senders) -
                             torch.index_select(mesh_pos, 0, receivers))
        edge_features = torch.cat((
            relative_world_pos,
            torch.norm(relative_world_pos, dim=-1, keepdim=True),
            relative_mesh_pos,
            torch.norm(relative_mesh_pos, dim=-1, keepdim=True)), dim=-1)

        mesh_edges = self.core_model.EdgeSet(
            name='mesh_edges',
            features=self._edge_normalizer(edge_features, is_training),
            receivers=receivers,
            senders=senders)

        if self.core_model == encode_process_decode_hub:
            return self.core_model.MultiGraphWithPos(node_features=self._node_normalizer(node_features, is_training), edge_sets=[mesh_edges], world_pos=world_pos, mesh_pos=mesh_pos)
        else:
            return self.core_model.MultiGraph(node_features=self._node_normalizer(node_features, is_training), edge_sets=[mesh_edges])

    def forward(self, inputs, is_training):
        graph = self._build_graph(inputs, is_training=is_training)
        if is_training:
            return self.learned_model(graph)
        else:
            return self._update(inputs, self.learned_model(graph))

    def _update(self, inputs, per_node_network_output):
        """Integrate model outputs."""

        acceleration = self._output_normalizer.inverse(per_node_network_output)

        # integrate forward
        cur_position = inputs['world_pos']
        prev_position = inputs['prev|world_pos']
        position = 2 * cur_position + acceleration - prev_position
        return position

    def get_output_normalizer(self):
        return self._output_normalizer

    def save_model(self, path):
        torch.save(self.learned_model, path + "_learned_model.pth")
        torch.save(self._output_normalizer, path + "_output_normalizer.pth")
        torch.save(self._edge_normalizer, path + "_edge_normalizer.pth")
        torch.save(self._node_normalizer, path + "_node_normalizer.pth")

    def load_model(self, path):
        self.learned_model = torch.load(path + "_learned_model.pth")
        self._output_normalizer = torch.load(path + "_output_normalizer.pth")
        self._edge_normalizer = torch.load(path + "_edge_normalizer.pth")
        self._node_normalizer = torch.load(path + "_node_normalizer.pth")

    def evaluate(self):
        self.eval()
        self.learned_model.eval()
