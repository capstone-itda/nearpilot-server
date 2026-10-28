param(
    [Parameter(Mandatory=$true, Position=0)]
    [ValidateSet('broker', 'collect', 'seed', 'server', 'sim', 'dashboard')]
    [string]$Action,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$ServerRoot = '@SERVER_ROOT@'
$DemoFolder = $PSScriptRoot
$ConfigPath = Join-Path $DemoFolder 'server.toml'
$ExitCode = 0
Push-Location -LiteralPath $ServerRoot
try {
    switch ($Action) {
        'broker' { & $Python -m tools.node_lab broker --dir $DemoFolder }
        'collect' { & $Python -c 'from nearpilot.cli import main; raise SystemExit(main())' serve --collect-only --config $ConfigPath }
        'seed' { & $Python -c 'from nearpilot.cli import main; raise SystemExit(main())' seed --config $ConfigPath (Join-Path $DemoFolder 'seed.toml') }
        'server' { & $Python -c 'from nearpilot.cli import main; raise SystemExit(main())' serve --config $ConfigPath }
        'sim' {
            $Mode = & $Python -c 'import json,sys; from pathlib import Path; print(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["mode"])' (Join-Path $DemoFolder 'lab.json')
            if ($LASTEXITCODE -ne 0 -or $Mode -ne 'sim') { throw '가상 기기는 별도 --mode sim 구성에서만 실행한다.' }
            & $Python -m tools.node_sim run --config (Join-Path $DemoFolder 'fleet.json')
        }
        'dashboard' {
            $Port = & $Python -c 'import sys; from nearpilot.runtime import load_config; print(load_config(sys.argv[1]).http.port)' $ConfigPath
            if ($LASTEXITCODE -ne 0) { throw '서버 설정을 읽지 못했다.' }
            $PreviousServer = $env:NEARPILOT_SERVER
            $env:NEARPILOT_SERVER = "http://127.0.0.1:$Port"
            try {
                Set-Location -LiteralPath (Join-Path $ServerRoot '../dashboard')
                & npm.cmd run dev -- --host 0.0.0.0
            } finally { $env:NEARPILOT_SERVER = $PreviousServer }
        }
    }
    $ExitCode = $LASTEXITCODE
} finally { Pop-Location }
exit $ExitCode
