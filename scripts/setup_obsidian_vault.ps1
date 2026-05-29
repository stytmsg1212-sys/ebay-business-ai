# W123 Phase C: Obsidian vault 構築 + junction 2 本
#
# 目的: vault を OneDrive 外に物理隔離し、memory / company を junction 経由で expose する.
#       OneDrive <-> Obsidian Git plugin の conflict copy 量産事故を防ぐ.
#
# 実行: PowerShell (admin 不要、New-Item -ItemType Junction で mklink /J 相当)
# 冪等性: 既存 junction を確認、target 一致なら no-op、不一致なら rebuild
# rollback: Remove-Item -Recurse $VAULT_ROOT
#
# 出典: .company/engineering/docs/2026-05-14-W123-W125_unified_design.md Decision A

$ErrorActionPreference = 'Stop'

# === 構成 (W126 後 path) ===
$VAULT_ROOT  = 'C:\Users\gucch\obsidian-vault'
$MEMORY_SRC  = 'C:\Users\gucch\.claude\projects\C--Users-gucch-projects-claude\memory'
$COMPANY_SRC = 'C:\Users\gucch\projects\claude\.company'

# === source 存在チェック ===
if (-not (Test-Path $MEMORY_SRC)) {
    throw "memory source not found: $MEMORY_SRC"
}
if (-not (Test-Path $COMPANY_SRC)) {
    throw "company source not found: $COMPANY_SRC"
}

Write-Host "=== W123 Phase C: Obsidian vault setup ===" -ForegroundColor Cyan
Write-Host "vault root : $VAULT_ROOT"
Write-Host "memory src : $MEMORY_SRC"
Write-Host "company src: $COMPANY_SRC"
Write-Host ""

# === vault root 作成 (冪等) ===
if (-not (Test-Path $VAULT_ROOT)) {
    New-Item -ItemType Directory -Path $VAULT_ROOT | Out-Null
    Write-Host "[CREATED] $VAULT_ROOT" -ForegroundColor Green
} else {
    Write-Host "[EXISTS]  $VAULT_ROOT"
}

# === junction 作成 helper (冪等) ===
function New-VaultJunction {
    param([string]$Link, [string]$Target, [string]$Label)

    if (Test-Path $Link) {
        $item = Get-Item $Link -Force
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            $currentTarget = $item.Target
            if ($currentTarget -eq $Target) {
                Write-Host "[EXISTS]  $Label junction (target match)"
                return
            } else {
                Write-Host "[REBUILD] $Label junction (target mismatch)" -ForegroundColor Yellow
                Write-Host "          old: $currentTarget"
                Write-Host "          new: $Target"
                # junction の Remove-Item は target 内容を消さない (link のみ削除)
                cmd /c rmdir "$Link"
            }
        } else {
            throw "$Link exists but is not a junction. Manual intervention required."
        }
    }

    New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null
    Write-Host "[CREATED] $Label junction: $Link -> $Target" -ForegroundColor Green
}

# === memory junction ===
New-VaultJunction `
    -Link "$VAULT_ROOT\memory" `
    -Target $MEMORY_SRC `
    -Label 'memory'

# === company junction ===
New-VaultJunction `
    -Link "$VAULT_ROOT\company" `
    -Target $COMPANY_SRC `
    -Label 'company'

Write-Host ""
Write-Host "=== verify ===" -ForegroundColor Cyan
Get-ChildItem $VAULT_ROOT -Force | Select-Object Name, Attributes, @{N='Target';E={$_.Target}} | Format-Table -AutoSize

Write-Host ""
Write-Host "[DONE] vault ready. Open in Obsidian: $VAULT_ROOT" -ForegroundColor Green
