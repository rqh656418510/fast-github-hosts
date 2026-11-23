# fast-github-hosts

在 Linux 平台自动定时更新 `/etc/hosts` 文件，解决 GitHub 国内访问慢的问题。`fast-github-hosts` 会从 `https://raw.hellogithub.com/hosts` 下载 hosts 条目，并在 `/etc/hosts` 中维护以下标记区块：

```text
##### FastGitHubHosts Start #####
140.82.112.26        alive.github.com
20.205.243.168       api.github.com
...
##### FastGitHubHosts End #####
```

每次运行都会用新下载的内容替换标记之间的内容，不做去重；标记不存在时，会在文件末尾创建区块。标记行必须完全一致（允许前后空白）；若标记缺失一端、重复、顺序错误、嵌套，或带有额外文本，脚本会报错并保持文件不变。 写入前会过滤下载内容：只保留主机条目；所有注释（整行注释和行内 `# ...` 注释，包括时间戳、URL 等）和空行都会被丢弃。

从旧版标记 `##### FastGitHub Start #####` 升级时，脚本只识别新标记，不会迁移或删除旧区块；请先按「卸载步骤」中的 `sed` 命令删除旧区块，否则旧条目会留在 `/etc/hosts` 中继续生效。

默认情况下，在发起网络请求前，脚本会检查目标 hosts 文件是否存在、是否可读、是否可写；如果目录不可写、目录属主不是当前用户/root，或目录可被其他用户写入且未设置 sticky，后续会回退到原地写入。检查失败或下载失败时会记录错误并退出，不继续更新文件。下载大小限制为 5 MiB，并会校验 IP 地址和主机名格式。下载按 UTF-8 解码（允许 BOM）；`/etc/hosts` 中原有的非 UTF-8 字节会原样保留。

更新通过同目录临时文件原子替换，原子替换失败时保留原文件；如果 `/etc/hosts` 是 Bind Mount（例如容器环境）导致 `rename` 返回 `EBUSY`，脚本会退化为原地重写，此时更新不是原子的，写入中断可能留下部分内容。如果 `/etc/hosts` 有多个硬链接，脚本也会原地重写以保留所有链接（同样不是原子替换）。内容与当前区块完全一致时不会写文件，`mtime` 保持不变。

真实更新时（包括 `--hosts-file` 指定的自定义文件），同一时间只允许一个实例运行：脚本会在 `/run/fast-github.lock` 上尝试非阻塞排他锁；如果另一个实例已经持锁，本次运行会跳过并返回 0。`--dry-run` 不加锁。如果锁文件无法打开（例如非 root 运行或 `/run` 不可用），脚本会记录警告后继续执行，此时不提供互斥保护。文件下载失败会重试（408、429 和 5xx 会重试，其余 4xx 不重试），并要求最终 URL 仍为 HTTPS；429/5xx 若返回 `Retry-After` 且不超过 10 秒，会按该值等待，超过则放弃本次重试。

日志同时写入 systemd journal 和本地文件 `/var/log/fast-github.log`。非 root 运行时只写 stderr/journal，不会创建或占用该文件。本地日志以防止符号链接攻击、非阻塞的方式打开，避免 FIFO 阻塞；若日志文件不是由服务用户拥有、或可被其他用户写入，脚本会改为仅通过标准错误输出日志。该日志文件不会自动轮转，长期运行请自行配置 `logrotate` 或定期清理。脚本仅使用 Python 标准库，需要 Python `3.6` 及以上版本和 Linux 环境（POSIX）。

## 配置文件

- `config/centos7/`：CentOS 7（systemd `219`）使用的 `fast-github-hosts.service` 和 `fast-github-hosts.timer`。
- `config/debian12/`：Debian 12（systemd `252`）使用的 `fast-github-hosts.service` 和 `fast-github-hosts.timer`。

两个目录里的 `unit` 名称相同，安装时只选择当前发行版对应的目录。

`fast-github-hosts.service` 是 `Type=oneshot`：每次运行结束就变为 `inactive (dead)`，因此定时任务的状态应查 `fast-github-hosts.timer`，每次运行的结果用 `journalctl -u fast-github-hosts.service` 查看。

## 安装步骤

以下安装命令都在项目根目录中执行，**只支持 Linux 系统（POSIX），不支持 Windows 系统**。

### CentOS 7 安装示例

如果 CentOS 7 系统尚未安装 Python 3，请先安装。CentOS 7 默认可能没有 `/usr/bin/python3`，请确认此路径可用；否则修改 `config/centos7/fast-github-hosts.service` 中的 `ExecStart`，指向实际安装的 Python 3。

- 安装文件：

```sh
# 安装脚本并设置可执行权限
sudo install -m 0755 fast-github-hosts.py /usr/local/bin/fast-github-hosts.py

# 安装 CentOS 7 的 systemd 服务
sudo install -m 0644 config/centos7/fast-github-hosts.service /etc/systemd/system/fast-github-hosts.service

# 安装 CentOS 7 的 systemd 定时器
sudo install -m 0644 config/centos7/fast-github-hosts.timer /etc/systemd/system/fast-github-hosts.timer

# 让 systemd 重新读取新增的配置
sudo systemctl daemon-reload
```

- 启用每小时定时任务，并立即手动运行一次服务：

```sh
# 启用定时器并立即启动
sudo systemctl enable --now fast-github-hosts.timer

# 立即执行一次服务，不必等待下一个定时周期
sudo systemctl start fast-github-hosts.service
```

- 查看定时器状态和运行日志：

```sh
# 查看定时器状态与下次触发时间
sudo systemctl status fast-github-hosts.timer

# 查看最近一次运行的输出日志
sudo journalctl -u fast-github-hosts.service

# 实时查看本地日志文件
sudo tail -f /var/log/fast-github.log
```

### Debian 12 安装示例

Debian 12 默认使用 systemd `252` 和 Python `3.11`，请先确认 `/usr/bin/python3` 可用，否则修改 `config/debian12/fast-github-hosts.service` 中的 `ExecStart`，指向实际安装的 Python 3。

- 安装文件：

```sh
# 安装脚本并设置可执行权限
sudo install -m 0755 fast-github-hosts.py /usr/local/bin/fast-github-hosts.py

# 安装 Debian 12 的 systemd 服务
sudo install -m 0644 config/debian12/fast-github-hosts.service /etc/systemd/system/fast-github-hosts.service

# 安装 Debian 12 的 systemd 定时器
sudo install -m 0644 config/debian12/fast-github-hosts.timer /etc/systemd/system/fast-github-hosts.timer

# 让 systemd 重新读取新增的配置
sudo systemctl daemon-reload
```

- 启用每小时定时任务，并立即手动运行一次服务：

```sh
# 启用定时器并立即启动
sudo systemctl enable --now fast-github-hosts.timer

# 立即执行一次服务，不必等待下一个定时周期
sudo systemctl start fast-github-hosts.service
```

- 查看定时器状态和运行日志：

```sh
# 查看定时器状态与下次触发时间
sudo systemctl status fast-github-hosts.timer

# 查看最近一次运行的输出日志
sudo journalctl -u fast-github-hosts.service

# 实时查看本地日志文件
sudo tail -f /var/log/fast-github.log
```

- Debian 12 与 CentOS 7 的主要配置差异：
  - CentOS 7 的 systemd `219` 不支持 `RandomizedDelaySec`，因此 `config/centos7/fast-github-hosts.timer` 使用 `AccuracySec=5min`，让 systemd 在 5 分钟窗口内触发，降低所有机器整点同时请求的压力。
  - Debian 12 的 systemd `252` 支持 `RandomizedDelaySec`，`config/debian12/fast-github-hosts.timer` 使用 `RandomizedDelaySec=300`，比 `AccuracySec` 提供更好的随机抖动。
  - 配置文件 `config/debian12/fast-github-hosts.service` 增加了 `ProtectSystem=yes`。它只把 `/usr`、`/boot`、`/efi` 挂成只读，`/etc` 仍然可写，脚本才能原子替换 `/etc/hosts`；CentOS 7 的 `unit` 没有这一项。
  - 两个 service 都使用 `Type=oneshot`、`User=root`、`TimeoutStartSec=180`、`NoNewPrivileges=yes`、`ProtectHome=yes`，并都以 `/usr/bin/python3` 运行。

### 修改定时更新的频率

- 编辑已安装的 `/etc/systemd/system/fast-github-hosts.timer` 中的 `OnCalendar`，然后执行：

```sh
# 重新加载 systemd 配置
sudo systemctl daemon-reload

# 重启定时器以应用新的执行频率
sudo systemctl restart fast-github-hosts.timer
```

如果要保留修改，则需同步更新仓库中的 `config/centos7/fast-github-hosts.timer` 或 `config/debian12/fast-github-hosts.timer`。两个发行版的默认抖动配置不同：CentOS 7 使用 `AccuracySec=5min`，Debian 12 使用 `RandomizedDelaySec=300`。

## 卸载步骤

- 执行卸载操作：

```sh
# 停用定时器
sudo systemctl disable --now fast-github-hosts.timer

# 删除已安装的文件
sudo rm -f /etc/systemd/system/fast-github-hosts.timer \
           /etc/systemd/system/fast-github-hosts.service \
           /usr/local/bin/fast-github-hosts.py

# 清理运行时锁文件
sudo rm -f /run/fast-github.lock

# 可选：删除本地日志
sudo rm -f /var/log/fast-github.log

# 重新加载 systemd 配置
sudo systemctl daemon-reload
```

- 如需同时删除 `/etc/hosts` 中的区块，先确认标记完整：

```sh
# 查看 FastGitHubHosts 标记所在行
grep -n FastGitHubHosts /etc/hosts
```

- 确认后再执行（如果某个标记只有一端，`sed` 会一直删到文件末尾）：

```sh
# 删除 /etc/hosts 中的 FastGitHubHosts 标记区块
sudo sed -i '/##### FastGitHubHosts Start #####/,/##### FastGitHubHosts End #####/d' /etc/hosts
```

## 手动运行脚本

- 基于 Python 3 手动运行脚本：

```sh
# 正常更新 /etc/hosts
sudo /usr/bin/python3 /usr/local/bin/fast-github-hosts.py
```

### 帮助：--help

- `--help` 可以查看全部脚本参数：

```sh
# 查看脚本帮助
/usr/bin/python3 /usr/local/bin/fast-github-hosts.py --help
```

### 预览：--dry-run

- 只从远端下载、校验并计算将要写入的内容，不修改系统的 `/etc/hosts` 文件：

```sh
# 预览将要写入的内容，不修改 hosts 文件
sudo /usr/bin/python3 /usr/local/bin/fast-github-hosts.py --dry-run
```

- 跳过写权限检查和 `/run/fast-github.lock` 全局锁。
- 日志会显示 `Would update ... N host entries would be written (dry run)`，并附上 unified diff（`Proposed changes:`，最多 200 行/64 KiB，超出会截断）；没有变化时显示 `already up to date (dry run)`。
- 预览成功返回 0；目标文件不存在、不是普通文件、下载失败或下载内容无效时返回 1；目标文件会在下载前检查。
- 因为不写系统 hosts 文件，普通用户也可以运行；非 root 运行时只写 stderr/journal，不会创建或占用 `/var/log/fast-github.log`。

### 指定 hosts 文件：--hosts-file

- 从远端下载、校验并写入 hosts 目标文件，指定的 hosts 目标文件必须已经存在（脚本不会创建它）；通常先复制一份 `/etc/hosts`：

```sh
# 复制一份 hosts 文件用于测试
cp /etc/hosts /tmp/hosts.preview

# 更新指定的 hosts 文件
sudo /usr/bin/python3 /usr/local/bin/fast-github-hosts.py --hosts-file /tmp/hosts.preview
```

- `--hosts-file` 参数的默认值是 `/etc/hosts`。
- 使用自定义路径做真实更新时，仍会尝试 `/run/fast-github.lock`；如果以非 root 运行且 `/run` 不可写，会记录警告并继续，此时没有互斥保护。`--dry-run` 不加锁。
- 可以配合非 `root` 用户使用，只要能读写目标 hosts 文件；非 root 真实更新会因无法打开锁文件而给出警告。
- 如果目标目录属主不是当前用户/root，或目录可被其他用户写入且未设置 sticky，脚本会改用原地写入，避免原子替换的 rename 竞态。
- 可以与 `--dry-run` 组合（同样需要先准备目标文件）：

```sh
# 复制一份 hosts 文件用于预览
cp /etc/hosts /tmp/hosts.preview

# 以 dry-run 模式预览指定文件并输出 diff
python3 /usr/local/bin/fast-github-hosts.py --dry-run --hosts-file /tmp/hosts.preview
```

## 运行单元测试

- 在项目根目录中执行单元测试：

```sh
# 运行 tests 目录中的全部单元测试
python3 -m unittest discover -s tests -v
```

- 测试使用临时目录中的 hosts 文件，不会修改系统的 `/etc/hosts` 文件。
- 测试依赖 POSIX 的 `fcntl`、`O_NOFOLLOW`、`geteuid` 和硬链接，需要在 Linux / POSIX 环境运行：
  - Linux：直接运行脚本。
  - Windows：可以使用 WSL（安装 `python3`）运行。

> Windows 平台还可以在 Git Bash / MSYS2 中使用 POSIX 版 `python3` 执行单元测试。需要注意，Git Bash 本身只是 Shell：如果它调用的是 Windows 原生 Python，没有 `fcntl` 接口，无法运行这套单元测试；需要让 Git Bash 指向 MSYS2 的 POSIX Python。

## 鸣谢

- [GitHub520](https://github.com/521xueweihan/GitHub520)
