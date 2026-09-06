"""LLM access: providers, credentials, and the batch job runner.

Terminology 术语:
    provider  提供商 —— 一个可调用的模型端点（base_url + model + 密钥）
    job       批次任务 —— 一次要跑几百上千次调用的长时间作业
    item      任务项 —— 批次里的一个调用单位，可单独重试
"""
