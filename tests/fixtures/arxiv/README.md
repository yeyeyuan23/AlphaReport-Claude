# arXiv 公告回归材料

2026-10-17 从官方真实响应提取，保留原始日期、数量和身份，没有生成虚构论文或分页。

- q-fin-recent.html： https://arxiv.org/list/q-fin/recent?skip=0&show=2000 。保留索引和全部真实论文编号，去掉无关页面装饰和正文。
- cs-lg-first-page.html： https://arxiv.org/list/cs.LG/recent?skip=0&show=25 。真实的306篇公告中的前25篇及日期索引。
- q-fin-metadata.xml：以上q-fin最新公告17个编号的元数据（保留返回版本），使用官方 https://export.arxiv.org/api/query 的id_list查询。
- 2610.21867v1.html： https://arxiv.org/abs/2610.21867v1 中的引用元数据、摘要和提交历史。

离线回归只回放响应，不访问网络、调用模型或发送飞书消息。
