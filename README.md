# 今天吃什么？🍽️

和朋友共享一个菜名池的随机选菜小工具：大家往里加想吃的菜，点一下按钮让命运决定今天吃什么。

- 纯 Python 标准库（`server.py`），Debian 服务器上**无需安装任何依赖**
- 菜名保存在 `dishes.json`，重启服务器/重启电脑都不丢
- 支持手动键入添加（逗号分隔可批量）、输入时下拉建议、搜索过滤已有菜名
- 所有人打开同一页面，改动每 5 秒自动同步

## 文件说明

| 文件 | 作用 |
|---|---|
| `server.py` | HTTP 服务器 + REST API（注册/登录、后台管理、静态页面、`/api/dishes`） |
| `index.html` | 前端页面（单文件，无需构建） |
| `admin.html` | 后台管理页（仅管理员可访问，`/admin`） |
| `dishes.json` | 菜名数据，含添加人与时间（首次运行自动创建） |
| `users.json` | 用户数据（PBKDF2 加盐哈希，不存明文密码；`role` 字段区分管理员） |
| `eatwhat.service` | systemd 服务单元（Debian 服务器用） |

## 账号

- 页面打开先注册：输入用户名（1-20 字符）和密码（4-64 位）即可，之后登录使用
- 每道菜会显示是谁添加的（标签上的小字，悬停可看时间）
- 右上角「账户设置」：修改自己的密码
- 自己添加的菜名标签上有 ✎（重命名）和 ×（删除），只能操作自己创建的菜品（管理员可在后台管理任意菜品）
- 登录会话存在服务器内存里，**服务器重启后需要重新登录**，菜名不受影响

## 后台管理

- 以 `role: "admin"` 的账号登录后，右上角会出现「后台管理」入口（或直接访问 `/admin`）
- 后台可查看所有注册用户（注册时间、添加菜数）与全部菜品（添加人、时间），按用户筛选
- 管理操作：删除某个菜品；删除某个用户（**该用户添加的菜品会一并删除**）；将用户设为/取消管理员；**重置任意其他用户的密码**（弹窗设置新密码，无需旧密码；对方当前会话不受影响，下次登录用新密码）
- 保护规则：不能修改自己的角色、不能重置自己的密码（自己的密码走前台「账户设置」）、不能删除管理员账号、至少保留一个管理员
- 开关「开放新用户注册」：关闭后前台隐藏注册入口，注册接口直接拒绝（状态存于 settings.json）
- 非管理员访问 `/admin` 或后台接口一律被拒绝（重定向回前台 / 403）

**创建或重置管理员账号**（在服务器上运行，会生成随机密码并打印）：

```bash
ADMIN_PW=$(openssl rand -hex 5) python3 - <<'EOF'
import hashlib, json, os, secrets, time
pw = os.environ['ADMIN_PW']
path = '/opt/eatwhat/users.json'
try:
    data = json.load(open(path, encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('users'), dict):
        data = {'users': {}}
except Exception:
    data = {'users': {}}
salt = secrets.token_hex(16)
users = data['users']
users['admin'] = {
    'salt': salt,
    'hash': hashlib.pbkdf2_hmac('sha256', pw.encode(), bytes.fromhex(salt), 100000).hex(),
    'created': int(time.time()),
    'role': 'admin',
}
json.dump(data, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('admin 密码:', pw)
EOF
```

如需换成其他用户名，把脚本里的 `'admin'` 改掉即可。

## 部署到 Debian 服务器

在**服务器上**执行（需要已安装 `git` 和 `python3`，Debian 一般都自带）：

```bash
# 1. 克隆代码
git clone https://github.com/SatoHideki1/eatwhat.git /opt/eatwhat
cd /opt/eatwhat

# 2. 安装并启动服务
cp eatwhat.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now eatwhat
```

然后访问 `http://服务器IP:8787` 确认服务正常，把这个地址（或你自己绑定的域名）发给朋友即可。

**创建管理员账号**：见上文「后台管理」一节的脚本（首次部署后需要运行一次，否则没有管理员）。

**从旧版（手动 scp 部署）迁移**：旧目录里的 `dishes.json` / `users.json` 是数据文件，先备份；按上面方式克隆到新目录（或直接在旧目录 `git init` 并关联远端后 `git fetch`），把数据文件复制回来，重启服务即可。

**如果开了防火墙**（如 ufw），记得放行端口：

```bash
sudo ufw allow 8787/tcp
```

## 日常维护

```bash
sudo systemctl status eatwhat    # 查看运行状态
sudo systemctl restart eatwhat   # 重启（更新代码后执行）
sudo systemctl stop eatwhat      # 停止
journalctl -u eatwhat -f         # 实时日志
```

**更新代码**：`cd /opt/eatwhat && git pull && systemctl restart eatwhat`。
`dishes.json` 和 `users.json` 在 `.gitignore` 里，`git pull` 永远不会覆盖它们，菜名和账号一直保留；备份这两个文件就是备份全部数据。

**换端口**：编辑 `/etc/systemd/system/eatwhat.service` 里 `ExecStart` 末尾的 `8787`，
然后 `sudo systemctl daemon-reload && sudo systemctl restart eatwhat`。

## 本地调试（可选）

```bash
python3 server.py            # 默认 8787 端口
python3 server.py 8899       # 指定端口
```

