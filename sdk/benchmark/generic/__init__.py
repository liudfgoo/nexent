# -*- coding: utf-8 -*-
"""Generic benchmark framework powered by Langfuse Datasets.

Provides a reusable experiment runner that bridges Langfuse's Dataset/Experiment
API with NexentAgent via the shared agent_runner.py execution engine.

Usage:
    from generic.run_experiment import run_benchmark
    run_benchmark(dataset_name="gsm8k-sample", evaluators=["em_f1", "exact_match"])
"""
