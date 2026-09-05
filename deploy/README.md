# TRTC 联调探针 · 部署与拆除

**这是临时探针，不是产品部署。** 验证完就拆，别让它长在服务器上。

## 现状（2026-09-05 已部署）

| 项 | 值 |
|---|---|
| 服务器 | 146.56.239.22 |
| 代码 | `/opt/voicekit/`（voice_kit + deploy + web + venv） |
| 配置 | `/etc/voicekit.env`（600, root）——**含 SDKSecretKey，最敏感** |
| 服务 | systemd `voicekit.service` → uvicorn 127.0.0.1:8010 |
| nginx | `qiuqiu.yongle.school` 里加了一条 `location /voicekit/` |
| 联调页 | https://qiuqiu.yongle.school/voicekit/test |

**跟球球老师生产完全隔离**：独立进程、独立端口、独立 systemd unit，
不进 `deploy.sh`、不重启 `qiuqiu.service`。

## nginx 那条 location 为什么必须自带 CSP

`qiuqiu.yongle.school` 的 server 级 CSP 是 `script-src 'self'; connect-src 'self'`，
会把 TRTC 的 SDK（`web.sdk.qcloud.com`）和 WebRTC 信令**全部挡掉**——
不看这一眼的话，页面会静默白屏，控制台里才有线索。

nginx 的规则是：**location 里只要有一条 `add_header`，就不继承 server 级的**。
所以给 `/voicekit/` 单独写一份宽松 CSP，生产页面那份严格的一个字没动
（已验证：`curl -I /` 仍是原来的严格策略）。

这也是它必须被拆掉的理由之一——一条宽松 CSP 挂在生产域名上，不该久留。

## 拆除（一条一条来）

```bash
ssh ubuntu@146.56.239.22
sudo systemctl disable --now voicekit.service
sudo rm -f /etc/systemd/system/voicekit.service && sudo systemctl daemon-reload
sudo rm -rf /opt/voicekit /etc/voicekit.env
# nginx：恢复备份（文件名里有 bak-voicekit- 时间戳）
ls /etc/nginx/sites-available/qiuqiu.yongle.school.bak-voicekit-*
sudo cp <上面那个备份> /etc/nginx/sites-available/qiuqiu.yongle.school
sudo nginx -t && sudo systemctl reload nginx
```

拆完记得**在腾讯云控制台重置 SDKSecretKey**——它在对话里传过，
而它能签出任意 userid 的 UserSig，等于能冒充任何孩子进房间。

## 已测到的数字（2026-09-05）

```
带闸门的端点（服务端实测 first_token_ms）  3235 / 3297 / 3326 ms   ← 很稳
直连 TokenHub 同模型同 prompt（无闸门）    7412 / 2589 / 2932 ms   ← 跳得厉害
```

**闸门的代价被高估了。** holdback=6 字，按流式吐字速度约 100~200ms；
上面那个几百毫秒的差是 LLM 自身波动淹掉的，不是闸门造成的。
真正该盯的是 LLM 首字本身在 2.6~7.4s 之间跳——这个波动比平均值更要命，
因为孩子等 7 秒和等 2.6 秒是两种完全不同的体验。

红线接管实测 `total_ms=0.7`，`took_over=true`，LLM 零调用。

## 还没测的

端到端（真人说话 → 听到球球出声）。需要人对着麦克风讲，
打开 https://qiuqiu.yongle.school/voicekit/test 点"开始对话"。
页面会显示「你说完 → 球球第一个字」和其中属于 LLM 的部分。
