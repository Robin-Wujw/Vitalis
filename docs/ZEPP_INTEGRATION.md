# Zepp 集成

[English](ZEPP_INTEGRATION.en.md)

本文档介绍浏览器配对、凭据处理、规范化数据覆盖范围、训练心率以及 Balance 2 桥接应用。

## 浏览器配对

Vitalis 通过用户的官方 Zepp 浏览器会话建立连接。账户密码和验证码仅提交至官方登录页面，Vitalis 绝不会接收或存储它们。

1. 打开 `https://<vitalis-origin>/api/v1/connect/zepp/scan?user=<local-user-id>`。
2. 按配对页面的说明安装 `browser_extension/` 中的扩展。
3. 在扩展中输入页面显示的 Vitalis 来源和一次性配对码。
4. 选择“登录并自动连接”；扩展会打开 Zepp 官方登录页面。
5. 成功完成官方登录后，扩展会提交可用的临时会话凭据，Vitalis 随即开始同步。

配对实现会：

- 从扩展 Cookie API 或官方页面的第一方 Cookie 视图中提取 `userid`、`apptoken` 和区域；
- 探测受支持的 Zepp 区域主机并选择可用区域；
- 在官方浏览器 Cookie 发生变化时更新凭据，并定期检查浏览器会话；
- 在持久化的同步尝试/分块账本中记录首次、续期、手动和计划同步；
- 使用可续期租约隔离同步尝试和分块的所有权，使租约已过期的工作进程无法提交结果或领取更多工作；
- 重启后，通过 ASGI 所拥有的调度器恢复已排队、等待重试、已过期以及明确取消的工作；
- 对暂时性网络/服务故障应用有界指数退避，同时保留已完成的分块；
- 仅在供应商明确拒绝身份验证后报告 `needs_login=true`；
- 签发独立的高熵浏览器链接令牌，并且仅存储其 SHA-256 摘要；
- 接受最长 730 天的同步窗口，并以有界分块获取历史记录；
- 分别报告八个数据流：心率、每日汇总、训练、训练详情、睡眠、HRV、健康状态和密集文件索引。

Cookie 暂时不可见或发生网络故障不会自动断开账户连接。扩展会调用 `/connect/zepp/link/validate` 验证服务端凭据状态，然后才会报告连接丢失。本地 Vitalis 用户在断开连接并清除该用户的数据之前，不能重新绑定到另一个 Zepp 供应商账户，从而防止两个供应商账户共享同一份分析历史。

## 凭据生命周期

供应商响应通常会报告：

| 字段 | 报告的时长 | 含义 |
| --- | --- | --- |
| `ttl` | 31,536,000 秒 | 浏览器登录会话生命周期 |
| `app_ttl` | 3,456,000 秒 | 应用令牌生命周期 |

这些是供应商响应字段，并非 Vitalis 可以使用的官方刷新凭据。仅当官方浏览器会话仍然有效且可读时，扩展才能更新应用令牌。退出登录、会话过期、密码变更或供应商风险控制都可能要求重新进行官方登录。

## 来源身份所有权

非空的 Zepp `userid` 只能有一个本地 Vitalis 所有者。数据库会在凭据行及其 Zepp 用户投影中强制执行唯一的 `(source, source_user_id)` 映射，同时每个 `(local user, source)` 只能有一条凭据记录。`AuthToken` 仍按来源限定，因此其他连接器可以存储自己的令牌，而不会覆盖 Zepp 投影。手动导入、首次配对和浏览器链接续期均使用同一个原子声明并保存操作。
有竞争关系的声明会返回 HTTP `409`；它不会替换任一用户的凭据，也不会合并其健康历史。

导入真实 Zepp 凭据时必须提供来自 `hm-user-login-info` 的供应商 `userid`。Vitalis 不再将新令牌与旧的后备身份组合使用。报告官方浏览器会话已断开只会将浏览器链接标记为需要登录；不会释放账户所有权，也不会丢弃服务端令牌。

在这些约束引入前创建的现有数据库必须在 API 或工作进程启动前完成迁移。停止所有 Vitalis 进程，制作并验证备份，然后运行：

```bash
python -m vitalis.storage.identity_migration audit
```

对于每个 `duplicate_identities` 组，请明确选择要保留 Zepp 凭据的本地用户：

```bash
python -m vitalis.storage.identity_migration resolve \
  --source zepp \
  --source-user-id <vendor-user-id> \
  --canonical-user-id <local-user-id> \
  --apply
```

选定的规范用户必须已经拥有匹配的令牌行。仅由过时的 `users.source_user_id` 投影表示的用户不能隐式接收其他用户的令牌。

如果 `duplicate_local_sources` 报告一个本地用户拥有多个令牌，请明确选择要保留的供应商身份：

```bash
python -m vitalis.storage.identity_migration resolve-local \
  --user-id <local-user-id> \
  --source zepp \
  --canonical-source-user-id <vendor-user-id> \
  --apply
```

没有供应商 ID 的旧版 Zepp 令牌会出现在 `missing_token_identities` 中。请在 Vitalis 外部验证身份，将其分配给经过审计的确切令牌行，然后解决其余本地重复项：

```bash
python -m vitalis.storage.identity_migration assign-missing \
  --token-id <audited-token-id> \
  --source-user-id <verified-vendor-user-id> \
  --apply
```

`mismatched_projections` 条目绝不会被隐式修复。验证保留哪个令牌后，请明确对齐用户投影：

```bash
python -m vitalis.storage.identity_migration resolve-projection \
  --user-id <local-user-id> \
  --source zepp \
  --source-user-id <retained-vendor-user-id> \
  --apply
```

`orphan_projections` 条目没有匹配的 Zepp 令牌，不能无限期保留供应商身份。验证其已过时后，仅清除该投影：

```bash
python -m vitalis.storage.identity_migration clear-projection \
  --user-id <local-user-id> \
  --source zepp \
  --source-user-id <stale-vendor-user-id> \
  --apply
```

然后规范化缺失的 Zepp 投影并创建唯一索引：

```bash
python -m vitalis.storage.identity_migration migrate --apply
```

解决过程绝不会合并或删除健康、训练、分析或反馈记录。对于每个非规范本地用户，该过程只会移除冲突的供应商凭据、清除供应商身份投影并撤销有效的浏览器链接。该用户的历史数据仍可通过原本的本地用户 ID 访问。操作员必须根据外部账户上下文选择规范所有者；Vitalis 绝不会根据记录数量或令牌新旧程度进行猜测。

## 规范化数据覆盖范围

Vitalis 目前会规范化：

- type-2 `/users/{id}/heartRate` 测量值，其编码形式为 `generatedTime` 加单字节 base64 `heartRateData` 值；在其采样间隔和时间戳方向得到验证之前，多字节载荷仍会被视为无法识别；
- `band_data.data_hr` 中的分钟级心率，每个本地日最多 1,440 个时段；
- 存在时的 RMSSD、SDNN、睡眠 HRV/RHR、就绪度组成项、皮肤温度差值、Charge、压力、SpO2/ODI、PAI、呼吸频率和乳酸阈值字段；
- 睡眠时长、时间、阶段、觉醒次数、评分、呼吸以及其他可用的供应商睡眠字段；
- 每日活动、静息心率、步数、活跃分钟数和训练汇总；
- 训练汇总，以及带类型的训练详情心率、速度、等效配速、步频、距离、海拔、跑步功率、圈、暂停和明确的力量训练组；
- 从密集 `SEC_HR` 云文件解码的秒级心率，并在样本旁保留按设备区分的覆盖范围和解码状态。

### 压力和 Charge

用户范围内的 `all_day_stress` 事件提供两项已验证的契约：

- 供应商每日汇总字段直接映射到 `stress`、`stress_min`、`stress_max` 以及四个 `stress_*_pct` 每日指标；
- 其 `data` JSON 数组提供带时间戳的 `stress` 指标样本。Vitalis 会保留每个明确的 UTC 时间戳、0-100 的供应商数值、缺口以及设备归属。由于该端点按 UTC 日划分事件，Vitalis 会在两端各请求一个填充日，再将样本裁剪回原始配置时区下的半开窗口。当前已连接账户的载荷显示出带缺口的五分钟采样间隔，但解析器不会假定或合成该采样间隔。

类别百分比会按报告原样存储。Vitalis 不会根据图表坐标轴推导供应商类别边界，不会用不完整的数据点重新计算每日汇总，不会诊断心理压力，也不会在恢复和训练决策中使用此序列。原始序列可通过 `/health/metrics/stress` 获取。

`Charge/real_data` 仍是独立的身体能量契约：`total`、`physical` 和 `mental` 分别映射到综合、身体和精神 Charge。`Charge/stress_data` 包含未记录的 protobuf 数据块，而 `Charge/insight_data` 包含未记录的洞察类型和系数。由于没有证据支持为其内部字段赋予健康语义，这两个子类型均不会进入常规同步。

每次同步都会获取密集索引，但仅当某个归档的索引新增了未见过的区间时才会下载该归档。已用有效归档检查但没有匹配样本块的区间会保留为 `no_data`，从而防止开放式的供应商占位项导致同一个每日归档被反复下载。

不同 HRV 来源不可互换：

- `readiness.sleepHRV` 是每台设备每晚一条的恢复汇总，恢复/基线决策会优先使用它；
- `hrv_sdnn/real_data` 是独立的稀疏 SDNN 事件流，并保有独立的基线；
- `HRVRMSSD/real_data` 是带时间戳且不连续的 RMSSD 事件流，仅用于能体现缺口的每日记录图表。在已连接设备上，它可能包含较长的睡眠时段，以及主睡眠区间之外仅有的短时段；其首尾时间戳不代表覆盖范围。

对于已连接账户，文件信息端点会以普通内联事件形式返回两种 HRV 事件类型，而不是可下载的归档描述符。目前只有 `second_heart_rate` 会返回 `fileId`/`fileType` 归档索引。因此 Vitalis 不会额外下载 HRV 文件，也不会从秒级心率归档推导 HRV。

UTC 测量值使用 `VITALIS_TIMEZONE`（默认为 `Asia/Shanghai`）归属到本地日。睡眠时间会保留供应商提供的本地偏移语义。

Zepp OS 公开目录提供 120 个当前活动 ID，另有两个公开的旧版 Huami 云历史 ID 单独映射。每种已知训练都会保留其供应商 ID、稳定模式、确切中文标签、训练类别、映射来源和识别置信度。未知或缺失的 ID 会保持显式状态，绝不会根据端点名称或描述性文本进行推断。

## 训练详情

Zepp 训练历史由 `/v1/sport/run/history.json` 返回；响应可能混合多种活动，因此每条记录的数值 `type` 才是权威依据。训练详情来自 `/v1/sport/run/detail.json`。Vitalis 将其压缩序列规范化为 `workout_metric_samples` 中带类型的 UTC 观测值。当前契约支持心率、速度、等效配速、步频、步幅、累计距离、海拔、跑步功率、触地时间、垂直振幅、垂直步幅比、圈、暂停，以及存在时供应商明确提供的力量训练组。三个 `runPosture` 哨兵值会被丢弃，而不是存为零。训练汇总会保留有效的六边界 `heart_range` 和 `heartrate_setting_type`，用于与设备一致的分区分析。

这是仅限训练的数据流，并非全天连续的高频心率。详情响应不会标明每个样本来自哪个传感器，因此规范化详情样本会保留 `source_scope=workout_detail` 和 `device_id=null`。训练汇总与 Balance 2 关联不足以证明其样本来自 Balance 2 或 Helio。

空字段会保持缺失。具体来说，力量训练可能包含秒级心率和供应商评估数据，却没有明确的动作组。在这种情况下，Vitalis 不会在连接器边界或智能层推断动作名称。它可以根据心率估算训练/休息结构，但在获得明确的供应商组或用户确认之前，动作和目标肌群保持未知。

`second_heart_rate/real_data` 返回文件索引而非样本。普通健康同步会存储这些索引而不下载大型归档。当明确请求 `decode_dense_files=true` 时，Vitalis 最多通过 Zepp 官方 `queryDownUrlList` 端点解码一个新归档。下载已签名的 HTTPS ZIP 时不会转发 `apptoken`，随后其 protobuf 心跳数据块会存储为设备范围内的心率样本。
每个数据块从 Unix 秒级时间戳开始，并包含连续的一秒级心率值；`255` 视为缺失。ZIP 条目通过全局一对一最大重叠匹配分配给已索引的设备。成功解码的索引行会存储 `parse_status=decoded` 和 `sample_count`；后续同步会跳过这些确切的文件/设备/时间行，因此不会反复下载历史文件。文件标识符保持私有，不会通过健康查询 API 暴露。

## Balance 2 桥接应用

`zepp_os/balance2_bridge/` 包含一个面向 Balance 2、采用 API_LEVEL 4.2 的 Zepp OS 应用。其后台服务是只追加回调日志的唯一写入方；前台页面拥有独立的确认检查点。逻辑待处理覆盖范围以最新 3,600 条回调记录为界，同时通过压缩和恢复计数器显示容量损失或存储损坏。Zepp OS 不提供 `fsync` 持久性保证。

手机端使用一次性显示的 Bearer 令牌，通过 HTTPS 上传固定的高水位批次。v2 端点会在指标事务提交后结算精确的客户端样本 ID，并单独标识永久验证拒绝项。网络、身份验证、协议或服务器故障会将未结算样本保留在原处。服务端的 `(user, source, metric, timestamp, source_scope, device)` 标识可确保已提交批次的重放具有幂等性。对于共享同一毫秒的回调，客户端会发送 `sample_ordinal`；持久化时会将该序数作为微秒级排序辅助值，同时在结算响应中保留原始毫秒值和序数。

回调频率由 Zepp OS 控制，并不声称固定为 1 Hz。桥接应用不会持续采集高功耗加速度计数据。Helio Strap 无法运行 Zepp OS 应用，因此此路径仅适用于 Balance 2。云同步仍是主要的历史数据来源。

构建和设备端设置说明位于
[`zepp_os/balance2_bridge/README.md`](../zepp_os/balance2_bridge/README.md)。

## 身份和设备边界

每个请求都使用明确的本地用户身份。供应商身份不会在不同本地用户之间被静默合并。指标会保留来源、来源范围、设备 ID 和单位；设备清单会将已验证的产品 ID 映射到 Balance 2 和 Helio Strap，而不会持久化设备身份验证材料。它们的 HRV 值保持分离：Vitalis 会将各个数据流与其自身基线比较，并且只融合由此得出的方向。当存在等价基线时，上臂证据可选择 Helio 作为显示数据流，但这不会覆盖跨设备分歧，也不代表 ECG 等效性。

性别、已确认 HRmax 和睡眠目标等用户生理信息会单独存储在带修订版本的 Vitalis `UserProfile` 中。当前区域 `apptoken` 客户端不会调用猜测得出的 Zepp 用户资料端点。历史 OAuth 用户资料字段以及 Zepp OS 设备资料/分区设置仅是未来可能采用的来源；它们绝不会覆盖 `USER_CONFIRMED` 值。训练最大心率和分区边界仍是观测值或设备设置，而不是已确认的 HRmax。

## 规范持久化与同步结果

训练历史可能会在运动端点、分页和重叠同步窗口之间重复出现同一天或同一场训练。Vitalis 会先更新或插入每个稳定的训练标识，然后使用 `VITALIS_TIMEZONE`，根据跨所有连接器来源的完整规范训练表重建每个受影响日期的 `training_records`。因此，分页顺序无法替换一天中的多场训练，来自不同来源的相同训练 ID 会保持分离，而修正后的开始时间戳会同时更新原本和新的本地日期。

带时间戳和每日指标的标识包含 `source`、`source_scope` 和 `device_id`；两个连接器、设备或语义不同的来源范围绝不会仅因其指标和时间相同就相互覆盖或聚合。原始、每小时、每日和稀疏每日 API 结果会公开这些来源信息。每日时间戳指标分桶使用 `VITALIS_TIMEZONE`；缺失的设备归属在 API 和分析边界仍保持缺失。

HTTP 200 空载荷、不受支持的端点、身份验证拒绝、暂时性网络/服务故障以及非空但无法识别的载荷是彼此独立的结果。仅明确为 `not_available` 的端点可以作为可选能力跳过，并记录为子数据流诊断信息。在同一能力内，所有分块必须全部可用或全部不可用：成功和不可用分块混合意味着覆盖范围不完整，并会阻止完整同步。在后续出现终止性错误之前完成的成功分块会先持久化，然后该数据流才会被标记为失败。明确的本地日期请求会使用 `VITALIS_TIMEZONE` 转回供应商日期参数，而不是使用其 UTC 边界日期。
真实的可选数据流或密集文件网络/身份验证故障会保留为失败，绝不会报告为空账户。身份验证失败会将浏览器链接标记为需要登录；暂时性故障会保持连接并可重试。
