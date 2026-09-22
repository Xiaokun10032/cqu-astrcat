# astrbot-plugin-helloworld

AstrBot 插件模板 / A template plugin for AstrBot plugin feature

> [!NOTE]
> This repo is just a template of [AstrBot](https://github.com/AstrBotDevs/AstrBot) Plugin.
> 
> [AstrBot](https://github.com/AstrBotDevs/AstrBot) is an agentic assistant for both personal and group conversations. It can be deployed across dozens of mainstream instant messaging platforms, including QQ, Telegram, Feishu, DingTalk, Slack, LINE, Discord, Matrix, etc. In addition, it provides a reliable and extensible conversational AI infrastructure for individuals, developers, and teams. Whether you need a personal AI companion, an intelligent customer support agent, an automation assistant, or an enterprise knowledge base, AstrBot enables you to quickly build AI applications directly within your existing messaging workflows.

# Supports

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Development Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)

#Todo

- [ ] 新增每日9点自动化查询所有已绑定用户的电费余额并记录，保留两天的数据，即今日凌晨与昨日凌晨的电费余额（KV）

- [ ] 新增两个用户自定义数据配置（KV）：是否开启电费余额不足提醒-BalanceRemind: bool，余额提醒阈值-BalanceRemindThreshold: float（>0）

- [ ] 新增一个命令设置这两个配置

- [ ] 根据今日9点凌晨电费余额是否低于在当日九点发送主动@消息提醒用户电费余额

- [ ] 记录昨日用电量，并在用户查询数据时返回

- [ ] 对返回查询结果的房间信息打码
