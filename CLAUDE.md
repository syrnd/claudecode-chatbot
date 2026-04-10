# CLAUDE.md

## 本项目

Telegram 机器人，驱动 Claude Code CLI。详见 README.md。

## aidlc-auto（新项目自动生成）

用户通过 Telegram 说"想做一个XXX"时，使用 aidlc-auto 系统。

### Skills 位置

```
~/project/aidlc-workflows/aidlc-auto/skills/
├── spec-gather.md      # 需求收集 → SPEC.md 生成
├── auto-pipeline.md    # GO 后全自动流水线
└── deploy.md           # 部署
```

### 流程

1. 用户说"想做一个XXX" → 读取 `~/project/aidlc-workflows/aidlc-auto/skills/spec-gather.md` 并遵循其指引
2. 多轮对话收集需求 → 生成 SPEC.md
3. 用户说 "GO" → 读取 `~/project/aidlc-workflows/aidlc-auto/skills/auto-pipeline.md` 并执行
4. 全自动：init.sh → 设计 → 实装 → 测试 → 审查 → 发布 → 部署

### 关键路径

- 初始化脚本: `~/project/aidlc-workflows/aidlc-auto/scripts/init.sh`
- SPEC 模板: `~/project/aidlc-workflows/aidlc-auto/templates/spec-template.md`
- 使用说明: `~/project/aidlc-workflows/aidlc-auto/USAGE.md`
