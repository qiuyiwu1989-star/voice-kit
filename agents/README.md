# agents/ · Agent 人设库（真源）

每个目录一个 agent，六层里五层是文件，第六层（铁律）是 `manifest.yaml` 里的 `rails` 档位，由平台注入。
改任何一层都升 `version`；`reports/` 是 testkit 跑出来的报告，评估卡从这里读。
后台 `/admin/agents` 只是这些文件的编辑器 + 校验器，不是另一份数据库。
标准见 球球老师/specs/014 §9–10 和「Agent 定义与验收标准」页。
