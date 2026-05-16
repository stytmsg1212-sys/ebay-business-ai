---
title: W132 ghost-session 耐性化 — transcript 無活動 reaper + 上限網 設計
date: 2026-05-16
author: Claude Code (Opus 4.7)
audience: W132 実装 session の assistant + user (承認判断)
sources:
  - https://code.claude.com/docs/en/hooks.md
  - https://code.claude.com/docs/en/remote-control.md
  - https://github.com/anthropics/claude-code/issues/35892
  - https://github.com/anthropics/claude-code/issues/17885
  - C:/Users/gucch/.claude/scripts/start-claude-loop.ps1
  - C:/Users/gucch/.claude/scripts/claude-loop.log (2026-05-16 実測: 幽霊 duration=34147s)
layer: wiki
updated: 2026-05-16
---

# W132 ghost-session 耐性化 設計 (B改: transcript 無活動 reaper + 上限網)

## 1. 概要

### 1.1 事象 (2026-05-16 実機確定)

`start-claude-loop.ps1` (watcher, PID 72288) が `claude.exe --remote-control --name ClaudeAutoLoop`
を子として起動 (PID 77712, 00:48:36)。user がスマホからリモート `/exit`。**しかし子プロセス
77712 は終了せず約 9.5 時間生存** (claude-loop.log: `claude exited (exit=-1, duration=34147s)`、
人手 kill するまで watcher は exit を検知できなかった)。

watcher の再起動ロジックは `start-claude-loop.ps1:122` の `while (-not $proc.HasExited)` 一点。
**OS プロセス生存 = セッション生存** という前提が `--remote-control` の常駐ホスト性質と不整合。
結果、自動再起動が一度も発火しなかった (機会損失: phone-driven 連続稼働の停止)。

### 1.2 根本原因 (報告ベースの一次情報、逐語は実装 P0 で再照合)

> 出典凡例 (M5 / R-2 適用): 下表は **claude-code-guide agent が公式 doc + GitHub issue を
> 調査し報告 (2026-05-16)**。逐語引用は本設計者が一次情報を直接確認したものではない
> ため、**実装 P0 で各 URL を再 fetch し「逐語・"not planned" ラベル・"~10 分" 値」を
> 一次照合**してから実装に進む (md-files-can-be-wrong / R-2: 推論を引用と混同しない)。

| 事実 (要旨) | 出典 (2026-05-16 報告、逐語は P0 再照合) | 帰結 |
|---|---|---|
| リモート `/exit` はリモート接続を閉じるのみ、ホスト `claude` は再接続待ちで生存 | code.claude.com/docs/en/remote-control.md | 「プロセス終了」待ちの watcher は永久に再起動しない |
| ネット到達不可が ~10 分継続でホストは timeout exit | code.claude.com/docs/en/remote-control.md | 幽霊化は「リモート切断 + ネット生存」固有 (ネット断時は自然 exit) |
| `/exit` では `SessionEnd` hook 非発火 (確実発火は Ctrl+D / logout) | github.com/anthropics/claude-code/issues/35892, /17885 (報告時点 "not planned") | 終了イベント駆動 (旧方針 A) は原理的に不成立 |
| `SessionEnd` payload に `--name`/PID なし (session_id/transcript_path/cwd のみ) | code.claude.com/docs/en/hooks.md | セッション識別困難、別端末セッション誤殺リスク |

### 1.3 旧方針 A (SessionEnd marker) を棄却した理由

A は「`/exit` 時に SessionEnd hook が marker を書く」前提だが、§1.2 の通り **`/exit` で
SessionEnd は発火しない** (公式 issue 2 件、修正予定なし)。土台が無いため棄却。
本設計は **watcher 側が幽霊を能動検知する polling 型** = 方針 B 改で確定 (2026-05-16 user 決定)。

## 2. 設計・方針

### 2.1 中核アイデア

watcher は spawn 時に子の transcript JSONL (会話ログ) を特定し、その **mtime が
`IdleRecycleMinutes` 分進まなければ「会話が止まった幽霊」と判定** → 子を kill →
内側ループ break → 既存の外側ループが再起動。

加えて **hard ceiling**: 単一子 PID が `HardCeilingHours` を超えて生存したら、transcript
判定の成否に関わらず強制 recycle。これは transcript 解決失敗・未知の盲点に対する
Q0 安全網 (無音延命を阻止し必ず log を残す。ただし「物理的に塞ぐ」とは言わない —
Kill 失敗時の残存経路は §5-R4 参照)。

### 2.2 なぜ transcript mtime が正しい signal か

Claude Code は user/assistant メッセージ・tool 呼出/結果を **逐次 JSONL に追記**する。
よって「N 分 mtime 不変」= 「会話 turn が N 分発生していない」。長時間の agentic task
(1 prompt で 30 分 tool 連打) でも tool イベントが逐次追記されるため mtime は進む
(= busy を idle と誤判定しない)。**この追記挙動は Phase 0 で実機確認する** (assumption、
バージョン依存リスクあり = md-files-can-be-wrong)。

### 2.3 transcript ↔ 子 PID の対応付け (cwd 非依存ヒューリスティック)

ClaudeAutoLoop の cwd は Startup folder 起動のため不定 (System32 等の可能性、§5 で要確認)。
cwd 依存の project-hash 計算は脆いので回避し、**spawn 時刻相関**で解決:

1. `Start-Process` 直後に `$start` (子 spawn 時刻) を記録
2. grace window (`TranscriptResolveGraceSec`, 既定 90s) 内で
   `~/.claude/projects/*/*.jsonl` を走査し、**作成時刻が `$start` より後**の jsonl を候補化
3. 候補が **ちょうど 1 件**ならそれを当該セッションの transcript として bind (1 回確定、
   以後 iteration では再解決しない = 別セッション jsonl への誤 latch 防止)
4. 候補が **0 件 / 2 件以上**なら bind せず **WARNING を claude-loop.log に記録**し、
   その子は **hard-ceiling のみで監視** (Q0: silent skip 禁止、degraded だが必ず痕跡)

> ⚠️ 同一 grace window 内に user が別 claude セッションを起動すると候補 2 件で
> hard-ceiling fallback に縮退する。実害は「その子だけ idle 検知が効かず最大
> `HardCeilingHours` 幽霊化」に限定 (無限延命はしない)。頻度低・影響限定のため
> K1 上この簡易版を一次案とする。Phase 0 で誤 bind 頻度を実測し、高ければ §6 の
> 堅牢版 (SessionStart hook + env marker state file) に昇格。

### 2.4 既存機構との共存

- **SessionStart watcher-of-watcher** (`session-start-load-incantation.sh` Step 4.5):
  heartbeat 60s stale で `start-claude-loop.ps1` を再 spawn する。**H1 訂正
  (Codex 2026-05-16)**: 実コードの cooldown は `if ($duration -lt 10) { Start-Sleep
  $CooldownSec }` = **短命 exit 専用**。reaper が kill する対象は idle≥30min /
  age≥12h の長命セッション (duration≥1800s) のため **cooldown は発火せず即時
  restart** になる (設計が cooldown に依存していると誤読しないこと)。staleness は
  「kill 直前の `Update-Heartbeat` (≤`HeartbeatIntervalSec`=10s) + bounded
  `WaitForExit` (≤`ChildKillWaitMs`=5s) + 外側ループ先頭の
  `Update-Heartbeat -phase starting`」で実測上 ≤ ~18s に収まり、60s 閾値を十分
  下回るため SessionStart 側は二重 spawn しない。**衝突しない (cooldown 非依存)**。
  **H2 緩和 (Codex 2026-05-16、user 承認)**: 外側ループ先頭の `phase=starting`
  heartbeat が staleness を担保するため、**kill 直前に追加 `Update-Heartbeat` を
  呼ぶ実装要件は課さない** (K1 最小、§3.3 擬似コードと整合)。
- **crash-loop guard** (`MaxRestartsPerHour=20`): 既存のまま流用、変更なし。
- **KillSwitch / 子 kill**: 既存の bounded wait パス
  (`WaitForExit($ChildKillWaitMs)`) を流用。ただし生 `$proc.Kill()` 直叩きは
  M4 で `Kill-ChildSafe` に置換 (新規 kill *戦略* は足さず安全 wrapper のみ =
  K1/K2、§3.1 / §3.3 footer と整合)。

### 2.5 変更しないもの (K2 surgical)

- `claude-loop.ps1` (旧版、未使用) には触れない
- settings.json / 既存 hook には触れない (一次案は単一ファイル変更)
- cwd は変更しない (ClaudeAutoLoop の業務文脈を壊さないため)

## 3. 詳細

### 3.1 変更ファイル (挙動変更は 1 ファイルのみ + ROADMAP bookkeeping)

> L7 注記 (Codex 2026-05-16): **挙動を変えるのは `start-claude-loop.ps1` の 1
> ファイルのみ** (K1)。`system_improvements.json` は ROADMAP status 更新で挙動
> 非変更のため別扱い。transcript bind/解決ロジックは §3.3 にインライン化し
> 専用 helper を作らない (K1)。helper は M4 の `Kill-ChildSafe` (kill 安全化) のみ
> 1 個追加 — bind 用 helper との混同に注意。

| Path | 変更 |
|---|---|
| `C:/Users/gucch/.claude/scripts/start-claude-loop.ps1` | config 追加 + `Kill-ChildSafe` helper 追加 + 内側 polling ループに transcript bind / idle / hard-ceiling 判定追加 |
| `tools/ebay-manager/data/system_improvements.json` (id 216) | status 更新 + 本設計書参照 (実装完了時、挙動非変更) |

### 3.2 config (先頭定数、§7 で値を user 確定)

```powershell
$IdleRecycleMinutes        = 30     # transcript 無成長で幽霊判定する分数 (user 政策決定)
$HardCeilingHours          = 12     # 単一子の最大生存時間、超過で強制 recycle (user 政策決定)
$TranscriptResolveGraceSec = 90     # spawn 後 transcript 特定を試みる猶予秒
$TranscriptGlob            = "$env:USERPROFILE\.claude\projects\*\*.jsonl"
```

### 3.3 内側ループ擬似コード (既存 while を最小拡張)

```powershell
$proc  = Start-Process -FilePath $claudeCmd.Source `
           -ArgumentList @('--remote-control','--name','ClaudeAutoLoop') `
           -WindowStyle Minimized -PassThru
$start = Get-Date
Write-Log "claude started (child PID=$($proc.Id))"

$transcript     = $null        # bind は 1 回だけ
$bindAttemptDeadline = $start.AddSeconds($TranscriptResolveGraceSec)
$bindResolved   = $false       # 解決 or 確定的に断念したか
$transcriptGone = $false       # M3: bound transcript 消失を 1 回だけ WARNING

# M4 (Codex 2026-05-16, user 承認): Kill() 自体が例外を投げても ghost を
# 孤児化させない。.Kill() 失敗時は taskkill /F に fallback (両方 log)。
function Kill-ChildSafe([System.Diagnostics.Process]$p, [string]$why) {
    try { $p.Kill() }
    catch {
        Write-Log "$why .Kill() failed: $_. taskkill /F fallback for PID=$($p.Id)..."
        try { & taskkill /F /PID $p.Id 2>&1 | Out-Null }
        catch { Write-Log "$why taskkill /F ALSO failed PID=$($p.Id): $_ (ghost may persist, logged)" }
    }
}

while (-not $proc.HasExited) {
    if (Test-Path $KillSwitch) { ...既存... ; break }

    # (A) transcript bind (grace window 内で 1 回確定)
    if (-not $bindResolved -and (Get-Date) -le $bindAttemptDeadline) {
        $cands = @(Get-ChildItem $TranscriptGlob -ErrorAction SilentlyContinue |
                   Where-Object { $_.CreationTime -gt $start })
        if ($cands.Count -eq 1) {
            $transcript = $cands[0].FullName; $bindResolved = $true
            Write-Log "transcript bound: $transcript"
        }
    } elseif (-not $bindResolved -and (Get-Date) -gt $bindAttemptDeadline) {
        $bindResolved = $true   # 断念確定 (以後 hard-ceiling のみ)
        Write-Log "WARNING: transcript unresolved (cands!=1) for PID=$($proc.Id). hard-ceiling only."
    }

    # (B) idle reaper (transcript 解決済の時のみ)
    if ($transcript) {
        if (Test-Path $transcript) {
            $idleMin = ((Get-Date) - (Get-Item $transcript).LastWriteTime).TotalMinutes
            if ($idleMin -ge $IdleRecycleMinutes) {
                Write-Log "idle-reap: PID=$($proc.Id) transcript idle ${idleMin}min >= $IdleRecycleMinutes. killing."
                Kill-ChildSafe $proc "idle-reap"
                break
            }
        } elseif (-not $transcriptGone) {
            # M3: bound transcript 消失 → idle 検知 degrade、痕跡を 1 回残す
            $transcriptGone = $true
            Write-Log "WARNING: bound transcript disappeared PID=$($proc.Id). idle-detect disabled, hard-ceiling only."
        }
    }

    # (C) hard ceiling (transcript 成否に関わらず常時)
    $ageHr = ((Get-Date) - $start).TotalHours
    if ($ageHr -ge $HardCeilingHours) {
        Write-Log "hard-ceiling: PID=$($proc.Id) age ${ageHr}h >= $HardCeilingHours. forced recycle."
        Kill-ChildSafe $proc "hard-ceiling"
        break
    }

    Update-Heartbeat -childPid $proc.Id
    Start-Sleep -Seconds $HeartbeatIntervalSec
    $proc.Refresh()
}
# 既存: bounded WaitForExit → exitCode → duration log →
#       (cooldown は duration<10s 時のみ = reaper kill では非発火) →
#       外側 while 先頭で Update-Heartbeat -phase starting → 再起動
# 実装注 (M4 一貫性): while 冒頭の KillSwitch kill も $proc.Kill() 直叩きを
#       Kill-ChildSafe $proc "killswitch" に置換すること
```

### 3.4 エッジケース

| ケース | 挙動 |
|---|---|
| transcript 解決前に子が正常 exit | 既存パス通り (HasExited→break→再起動)、reaper 不介入 |
| bound transcript が削除 | 初回検知時に WARNING を 1 回 log → idle 判定 degrade、hard-ceiling は継続 (無限延命は阻止、痕跡あり。最大 `HardCeilingHours` は idle 検知欠落 = §5-R2) |
| grace window 内に候補 2 件 | bind 断念 + WARNING log、hard-ceiling のみで監視 |
| reaper kill 直後に SessionStart watcher-of-watcher 発火 | reaper 対象は長命=cooldown 非発火=即時 restart。直前 hb(≤10s)+WaitForExit(≤5s)+外側先頭 phase=starting hb で staleness ≤~18s < 60s → 二重 spawn しない (cooldown 非依存) |
| idle 閾値直前で user が再接続し会話再開 | transcript mtime 前進 → 次 iteration の `$idleMin` が `LastWriteTime` から再計算され閾値未満に戻る (stateless、recycle されない) |

### 3.5 ログ (Q0 トレーサビリティ)

全 recycle・degrade を `claude-loop.log` に理由付きで記録: `idle-reap` /
`hard-ceiling` / `transcript unresolved WARNING` (bind 候補≠1) /
`bound transcript disappeared WARNING` (M3) / `.Kill() failed → taskkill fallback`
(M4) / `taskkill ALSO failed` (ghost 残存し得るが log 済)。**痕跡なき skip を
作らない** (Q0)。idle 検知が degrade する経路 (transcript 消失・未解決) も
hard-ceiling が必ず log+kill するため**無限延命はしない** (検知精度は落ちるが無音
ではない)。

## 4. 検証計画 (Q1 DoD を infra 向けに adapt)

| Phase | 内容 | 合否 |
|---|---|---|
| **P0 前提確認** | (1) ClaudeAutoLoop の実 cwd と project-hash dir 特定 (2) test ClaudeAutoLoop で multi-tool prompt 実行中に jsonl mtime が逐次進むか実測 (busy≠idle 検証) | mtime が tool 実行中も進めば §2.2 assumption 成立 |
| **P1 模擬** | 古い (stale) jsonl に bind させ idle 閾値超で kill→再起動 / 成長中 jsonl では非発火、を低閾値設定で確認 | log に `idle-reap`、新子 PID 起動 |
| **P2 実機 (user 操作)** | ClaudeAutoLoop 起動 → スマホから実際にリモート `/exit` → `IdleRecycleMinutes` 内に `idle-reap` log + 旧 PID kill + 新 PID 起動 + 新 transcript bind + heartbeat 更新 | 5 点全て実機確認 (R-11 準拠で user 視認まで) |
| **P3 上限網** | `HardCeilingHours` を低値に設定し、active transcript でも強制 recycle + log されるか | log に `hard-ceiling` |

実機ログ抜粋を完了報告に添付 (pytest 不在の infra のため scheduler.log 相当 = claude-loop.log)。

## 5. リスク / 残課題

> ※ R 番号は登録順 (R4/R4b は M4 関連でグルーピング)。severity は各項目の括弧内を
> 参照 (番号順 ≠ severity 降順)。

- **R1 (中)**: idle 閾値超で離席中の正常リモートセッションが recycle される。
  → 受容判断 (本機構の目的が「常に fresh セッションを用意」であり、長時間放置
  セッションの破棄は概ね意図と一致)。閾値 `IdleRecycleMinutes` で緩和。**user 政策決定**。
- **R2 (中)**: transcript JSONL の path scheme / 逐次追記挙動がバージョン依存で変わると
  解決失敗。→ hard-ceiling で無限延命は阻止 (degraded 安全)。P0 で実測、変化検知時は §6 昇格。
- **R3 (低)**: 同時刻に別 claude 起動で誤 bind 候補増 → hard-ceiling fallback に縮退
  (実害限定、無音化なし)。
- **R4 (中、Codex M4 / 2026-05-16)**: kill 失敗時の残存 ghost。`$proc.Kill()` が
  例外 (host への access denied 等) を投げると ghost 生存のまま `break` → 外側
  while が新子を spawn し ghost が孤児化、本 loop は同 ghost を再 kill しない。
  緩和: M4 採用で `Kill-ChildSafe` が `.Kill()` 失敗時に `taskkill /F /PID` を
  fallback 実行 (両方 log)。**taskkill も失敗した場合のみ ghost 残存**し得る
  (log 済 = 無音ではない / 当該孤児のみ、次回 loop の別子には波及せず)。
  ⇒ 「hard-ceiling が無限延命を物理的に塞ぐ」は **「log を残しつつ二重 kill 経路で
  実質ほぼ塞ぐ」** に弱体化 (§2.1 と整合)。
- **R4b (低)**: `$proc.Kill()` が host の子孫 (node 等) を残す可能性 → 既存
  `WaitForExit($ChildKillWaitMs)` で bounded、未 exit は exit=-2 で log 済 (既存踏襲)。

## 6. 昇格パス (P0 で簡易版不十分と判明時のみ)

SessionStart hook は (`/exit` と違い) 確実発火する。watcher が spawn 前に
`$env:CLAUDE_AUTOLOOP=1` を設定 → settings.json に最小 SessionStart hook を 1 本追加し、
`$CLAUDE_AUTOLOOP` 検出時のみ `{session_id, transcript_path}` を
`claude-loop.session.json` に記録 → watcher がそれを読み厳密 bind。
**+1 hook / settings.json 変更を伴うため一次案では採用せず** (K1)。P0/P2 の誤 bind 実測が
高頻度の場合のみ user 承認の上で昇格。

## 7. 未確定 (user 政策決定 — 承認時に確定)

1. **`IdleRecycleMinutes`** (既定提案 30): スマホで離席して戻る想定の最大放置時間。
   短い=幽霊回収速いが離席に弱い / 長い=離席に強いが幽霊が長居。
2. **`HardCeilingHours`** (既定提案 12): 単一セッション最大生存。長い連続作業を
   する運用なら 24、頻繁に fresh が欲しいなら 6。`IdleRecycleMinutes` より十分大。
3. P0 結果次第で §6 堅牢版に昇格するか。

## 8. 再発防止

- 「プロセス生存 = セッション生存」前提を恒久的に否定。watcher は今後 **活動 signal
  (transcript) ベース**で生存判定する設計に統一。
- 「mobile /exit→自動再起動 動作実証済」(旧 memory) は **クリーンなリモート /exit を
  実証していなかった** (md-files-can-be-wrong 事例)。P2 で真のリモート /exit を実証し、
  session memory に「実証した exit 経路」を明示記録する。
- 本設計の前提 (transcript 逐次追記・path scheme) は P0 で実測してから実装 (K0/K3)。
