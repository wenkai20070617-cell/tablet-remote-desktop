# Tablet Remote Desktop

这是一个让平板浏览器远程控制 Windows 电脑的个人远程桌面应用。

## 当前口径

| 项 | 内容 |
|---|---|
| 被控端 | Windows 11 电脑 |
| 控制端 | 平板浏览器 |
| 公网入口 | Cloudflare Tunnel |
| 本地认证 | 二次密码 |
| 声音 | 支持，依赖 Windows loopback 采集 |
| 剪贴板 | 支持文本同步 |
| 文件传输 | 不做 |
| 多显示器 | 不做 |
| 开机自启 | 不做 |

## 运行

在电脑上打开 PowerShell，进入本目录：

```powershell
cd path\to\tablet-remote-desktop
.\start.ps1
```

第一次启动会创建本地 Python 环境、安装依赖并生成本地登录密码。终端会打印密码。

本机测试地址：

```text
http://127.0.0.1:18765
```

如果忘记本地密码，停止服务后运行：

```powershell
.\reset_password.ps1
```

## 公网访问

服务启动后，再打开一个 PowerShell，运行：

```powershell
cd path\to\tablet-remote-desktop
.\run_cloudflare_tunnel.ps1
```

脚本会启动 Cloudflare Quick Tunnel，并输出一个 `https://...trycloudflare.com` 链接。平板打开这个链接后输入本地密码即可连接。

## 安全说明

Quick Tunnel 只提供随机公网链接，本项目本地服务会再要求密码。固定域名和 Cloudflare Access 需要你在 Cloudflare 后台配置，不能由本地脚本单独完成。

`config.json` 会在首次运行时生成，其中包含本机密码哈希和盐值，已通过 `.gitignore` 排除，禁止提交到公开仓库。`.venv`、运行日志和下载的 `cloudflared.exe` 同样不会进入版本控制。

建议正式使用时配置：

| 项 | 要求 |
|---|---|
| Cloudflare Access | 只允许你的邮箱访问 |
| 本地密码 | 使用强密码 |
| 电脑端状态 | 使用时保持终端窗口可见 |
| 停止远控 | 关闭 PowerShell 服务窗口 |

## 限制

| 项 | 说明 |
|---|---|
| UAC/管理员窗口 | 普通权限程序可能无法控制高权限窗口 |
| Ctrl+Alt+Del | 浏览器和普通输入注入不能可靠发送安全注意序列 |
| 声音 | 依赖本机音频设备支持 loopback |
| 性能 | 当前是浏览器 JPEG 帧流，适合个人控制，不是游戏/高帧率视频方案 |
