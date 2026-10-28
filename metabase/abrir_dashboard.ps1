<#
Abre o dashboard Swfast como um aplicativo do Windows.

Uso do cliente: dois cliques no atalho "Dashboard Swfast" da área de trabalho.
O atalho roda este script escondido, que:
  1. confere se o Metabase responde em 127.0.0.1:3000;
  2. se não responder, liga os containers do banco e do Metabase e espera
     (até 3 min) — funciona com Docker no Windows e com Docker dentro do WSL;
  3. abre o Edge em modo aplicativo (janela própria, sem barra de endereço),
     no link público do dashboard, que se atualiza sozinho a cada 10 min.

Instalação (uma vez, no Windows, logado como o cliente, depois do
`docker compose run --rm instalar-dashboard` ter gerado data\dashboard_url.txt):
  powershell -ExecutionPolicy Bypass -File metabase\abrir_dashboard.ps1 -Instalar
Copia este script e o link para %LOCALAPPDATA%\SwfastDashboard e cria o atalho.
#>
param(
    [switch]$Instalar,
    # Onde está o dashboard_url.txt na instalação (padrão: data\ do repositório).
    [string]$ArquivoUrl = "",
    # Distribuição do WSL onde roda o Docker Engine (produção).
    [string]$Distro = "Ubuntu"
)

$ErrorActionPreference = "Stop"
$Pasta = Join-Path $env:LOCALAPPDATA "SwfastDashboard"
$Saude = "http://127.0.0.1:3000/api/health"
$Containers = @("swfast-postgres", "swfast-metabase")
$EsperaMaximaSegundos = 180

function Mostrar-Erro([string]$Mensagem) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($Mensagem, "Dashboard Swfast", "OK", "Warning") | Out-Null
}

function Metabase-Responde {
    try {
        $resposta = Invoke-RestMethod -Uri $Saude -TimeoutSec 5
        return $resposta.status -eq "ok"
    } catch {
        return $false
    }
}

function Ligar-Containers {
    # Docker no próprio Windows (Docker Desktop) ou dentro do WSL (Docker Engine).
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        & docker start @Containers 2>$null | Out-Null
    } else {
        & wsl.exe -d $Distro -- docker start @Containers 2>$null | Out-Null
    }
}

function Encontrar-Edge {
    $candidatos = @(
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
    )
    foreach ($caminho in $candidatos) {
        if (Test-Path $caminho) { return $caminho }
    }
    return $null
}

function Instalar-Atalho {
    $origemUrl = if ($ArquivoUrl) { $ArquivoUrl } else { Join-Path $PSScriptRoot "..\data\dashboard_url.txt" }
    if (-not (Test-Path $origemUrl)) {
        throw "Não achei $origemUrl. Rode antes: docker compose run --rm instalar-dashboard"
    }
    New-Item -ItemType Directory -Force -Path $Pasta | Out-Null
    Copy-Item -Force $PSCommandPath (Join-Path $Pasta "abrir_dashboard.ps1")
    Copy-Item -Force $origemUrl (Join-Path $Pasta "dashboard_url.txt")

    $atalho = Join-Path ([Environment]::GetFolderPath("Desktop")) "Dashboard Swfast.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($atalho)
    $lnk.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $Pasta 'abrir_dashboard.ps1')`""
    $lnk.WorkingDirectory = $Pasta
    $lnk.IconLocation = "$env:SystemRoot\System32\imageres.dll,144"  # ícone de gráfico do Windows
    $lnk.Description = "Abre o dashboard de vendas Swfast"
    $lnk.WindowStyle = 7  # minimizado: o PowerShell não pisca na tela
    $lnk.Save()
    Write-Host "Atalho criado em $atalho"
}

function Abrir-Dashboard {
    $arquivo = Join-Path $Pasta "dashboard_url.txt"
    if (-not (Test-Path $arquivo)) { $arquivo = Join-Path $PSScriptRoot "dashboard_url.txt" }
    if (-not (Test-Path $arquivo)) {
        Mostrar-Erro "O dashboard ainda não foi instalado neste computador. Chame o suporte."
        return
    }
    $url = (Get-Content $arquivo -Raw).Trim() + "#refresh=600"

    if (-not (Metabase-Responde)) {
        Ligar-Containers
        $inicio = Get-Date
        while (-not (Metabase-Responde)) {
            if (((Get-Date) - $inicio).TotalSeconds -gt $EsperaMaximaSegundos) {
                Mostrar-Erro "O dashboard não respondeu. Reinicie o computador e tente de novo; se continuar, chame o suporte."
                return
            }
            Start-Sleep -Seconds 3
        }
    }

    $edge = Encontrar-Edge
    if ($edge) {
        # Perfil próprio: abre como uma janela de aplicativo, separada do navegador do cliente.
        $perfil = Join-Path $Pasta "perfil-edge"
        Start-Process -FilePath $edge -ArgumentList "--app=$url", "--user-data-dir=`"$perfil`"", "--start-maximized", "--no-first-run"
    } else {
        Start-Process $url  # sem Edge: navegador padrão
    }
}

try {
    if ($Instalar) { Instalar-Atalho } else { Abrir-Dashboard }
} catch {
    if ($Instalar) { throw }
    Mostrar-Erro "Não foi possível abrir o dashboard: $($_.Exception.Message)"
}
