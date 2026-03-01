---
trigger: always_on
---

在开发过程中使用以下命令进行快捷调试：

快速一键重新打包部署
./deploy/apply.sh

检查运行日志
docker compose logs --tail=200 nanobot-gateway

凡是配置更新都需要在deploy/config.template.json 目录为我更新配置为默认值