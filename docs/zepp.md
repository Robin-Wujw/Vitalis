# Zepp 数据接入与解析证据

[文档导航](README.md) | [首次配对](quickstart.md) | [数据合同](data-contracts.md)

## 连接和身份

真实账号的首选路径是由具有 `manage` 权限的本地用户创建一次性配对码，浏览器扩展只在 Zepp 官方页面读取 `userid`、`apptoken` 和区域，再通过配对码提交；服务端会核对请求 `Origin`，扩展来源须在 `VITALIS_PAIRING_ALLOWED_ORIGINS` 中明确配置；Chrome 扩展访问本机 API 的私有网络预检仅对已列入白名单的来源放行，不能把允许私有网络当作免鉴权。密码和验证码不送往 Vitalis。浏览器会话可用时扩展可更新应用令牌；退出登录或会话过期可能需要重新登录。Zepp 供应商身份只归属一个本地用户，冲突不得通过静默合并健康记录解决。配对页面的 HTTPS/访问控制边界见[安全说明](../SECURITY.md)，扩展的独有安装方式见[扩展 README](../clients/browser_extension/README.md)。

模拟 OAuth state 也有短 TTL，回调消费使用数据库条件删除并返回绑定用户，重复或过期回调不会再次成功。凭据更新会推进 `SourceAccount.fence_epoch`，取消旧的在途同步并拒绝旧 worker 的终端投影；撤销会同时撤销当前配对码和浏览器登录链接，保留历史健康事实。重新配对仍须通过新的当前用户配对码。

具有 `manage` 权限的客户端可调用唯一的 `POST /api/sources/zepp/revoke` operation 断开数据源。撤销会删除可用厂商凭据、撤销浏览器登录链接并取消在途同步，但保留本地用户和历史事实；账号的 fencing epoch 会递增，因此旧 worker 不能在撤销后或重新配对后提交结果。重新配对仍须通过当前用户的配对码，不能因历史任务或旧凭据自动创建已删除的本地用户。

## 取得的事实与限制

云端同步覆盖睡眠、每日活动、心率、HRV、压力、训练汇总和有界训练详情，密集 `second_heart_rate` 文件先建立索引；按明确选项才下载并解码为按设备隔离的秒级心率。不同 Zepp HRV 类型（睡眠汇总、SDNN、带时间戳 RMSSD）不可互换，秒级心率也不是逐搏 HRV。`all_day_stress` 的每日汇总和带显式时间戳的 `data` 样本分别保存；缺口不补，未验证的 `Charge/stress_data` 和 `Charge/insight_data` 不作为健康事实。

训练身份保留 `source` 和供应商 `workout_id`；历史运动的数字 `type` 必须按已核验的云端编号空间解释，不能套用 Zepp OS 的活动编号。`/v1/sport/run/detail.json` 的已知字段才进入当前 `WorkoutDetail 5.1`。力量训练组优先使用有效 `strengthSets`；只有 `training_family=strength` 且分圈恰为 62 列时，`lap_62` 才读取 0-based 第 21、22、28 列的重量原数值、正整数次数及动作代码，保留顺序，并进入 `observed_sets`。供应商原始负重未知单位不补 `kg`，未知动作代码不猜名称，也不将这些观测当作用户确认动作或处方依据。

动作名称的唯一运行时来源是 [strength_exercises.json](../src/vitalis/adapters/zepp/data/strength_exercises.json)。其 `zepp.strength.lap_62` 命名空间仅含既有训练中观测到的 26 个代码；已核验标签有应用版本、APK 摘要、中文目录响应摘要和历史观测引用。离线 APK 容器 SHA-256 为 `64e1d87b6ab79e0ecedfbead176cc6bb781f9754b46915d52c0d2ccfbcef5949`；Zepp App `10.8.7-play` 将 `lap_62` 第 28 列经 `strengthTrainType` 对到区域 `/sport/config/muscle` 的 `actionType` / `actionName`，当时中文响应 SHA-256 为 `7e6b4ba617bd8e61b0c269c9c51fb4d7b430c0e30332273a964fa88f8028abeb`。这不是完整厂商目录，也不能证明用户在 App 中修改后的逐组动作。未知代码返回原 code 和 `unmapped`，未经证实的 `provisional` 名称不进入权威显示映射；目录已知、厂商明确组和用户确认仍是三个不同维度。

用户提供的截图显示 App 的逐组与压力展示，但图片本身不能证明动作数值 code 与名称的关联，也不能推导压力类别阈值，因此未把截图臆测出的新映射加入目录。低资源环境的旧训练详情补采受请求、样本与 worker 内存预算约束；每次有界补采不证明全部历史详情齐全。夜间、晨晚报及周月报同步中，超过解析资源上限的可选训练明细会记为 `unavailable/resource_limit`，保留未同步状态并继续排队分析已有事实；手动同步仍明确报告该明细失败。维护者须使用[运维](operations.md)中的备份和账本核对，不得直接对旧日期批量倒序重算当前健康事件快照。

## 同步状态

`ZEPP_MOCK=true` 的 HRV 分块只生成合成 SDNN 样本：按请求的本地日期和持久化时区选择 UTC 采样时刻，使用可由当前逐样本解析器识别的 `startTime` / `samples[].sdnn`，未提供设备 ID 时保持归属未知。同一窗口重复读取返回相同样本。其它 mock 事件按各自合同提供日期或样本时间、显式指标字段与已知单位的合成 DailyHealth、Charge、readiness、呼吸、RMSSD 与乳酸阈值观测；设备清单使用明确的模拟标识，watch 统计与训练汇总只覆盖请求窗口，训练详情仅返回身份可核对的空明细。密集文件、压力、SpO2 与 PAI 等未模拟的流明确返回空列表，不伪造文件或生理读数。上述行为只用于隔离的全分块同步/晨晚报验收，不代表真实 Zepp 载荷、设备测量或云端历史覆盖已通过实测。

一次空查询、明确不支持、未识别载荷、认证拒绝与网络故障属于不同结果；已成功保存的分块不因后续失败而被抹去。训练来源是否完整须以成功分页和同步账本为证，未知日期不能说成没有训练。训练历史只有已校验的空列表或明确终止游标才能证明分页结束；非空有效页缺少 `data.next` 时保留已观测行并标记 `partial`，不把未知载荷当作完整历史。HTTP 重试受当前请求剩余时间、30 秒切片和取消检查共同约束；恢复任务继续使用持久化尝试时区计算 HRV 的本地日边界，不回退到 worker 进程时区。需要了解源流覆盖、重试和资源预算时使用[运维诊断](operations.md)；字段和响应以运行服务的 `/docs` 为准。
