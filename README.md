# 局域网云盘

本机共享文件夹，局域网内输入密码后上传/下载。

## 启动

双击 `start.bat`，或：

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

- 本机：http://127.0.0.1:8091  
- 局域网：http://你的电脑IP:8091  
- 首次使用：复制 `config.example.json` 为 `config.json` 并修改密码等配置

## 配置 `config.json`

| 字段 | 说明 |
|------|------|
| `port` | 端口，默认 `8091` |
| `password` | 共享密码 |
| `storage_dir` | 文件存放目录，默认 `D:\云盘数据` |
| `max_upload_mb` | 单文件上限（MB），`0` 表示不限制 |

改配置后重启服务生效。
