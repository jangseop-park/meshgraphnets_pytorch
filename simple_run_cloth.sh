#!/bin/bash
#SBATCH --job-name=cloth_simple_run
#SBATCH --partition=gpu_8


srun --exclusive -N1 -p gpu_8 --gres=gpu python run_model.py --model=cloth --mode=all --rollout_split=valid --dataset=flag_simple  --epochs=5 --trajectories=1000 --num_rollouts=100 --core_model=encode_process_decode --message_passing_aggregator=sum --message_passing_steps=5 --attention=False --ripple_used=False --ripple_generation=equal_size --ripple_generation_number=1 --ripple_node_selection=random --ripple_node_selection_random_top_n=1 --ripple_node_connection=most_influential --ripple_node_ncross=1 --use_prev_config=True