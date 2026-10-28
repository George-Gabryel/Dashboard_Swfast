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
Copia este script e o link para %USERPROFILE%\SwfastDashboard e cria o atalho.
#>
param(
    [switch]$Instalar,
    # Onde está o dashboard_url.txt na instalação (padrão: data\ do repositório).
    [string]$ArquivoUrl = "",
    # Distribuição do WSL onde roda o Docker Engine (produção).
    [string]$Distro = "Ubuntu"
)

$ErrorActionPreference = "Stop"
# Fora do AppData de propósito: um programa instalado pela Microsoft Store
# (como o app do Claude, ou um terminal aberto por ele) que grava em AppData
# tem a gravação desviada para uma pasta só dele — o atalho, aberto pelo
# Explorer, não acharia o script. A pasta do usuário não sofre esse desvio.
$Pasta = Join-Path $env:USERPROFILE "SwfastDashboard"

# Qualquer erro fora do try final (ex.: ao preparar o script) também vai
# para o log — rodando escondido, sem isso a falha seria silenciosa.
trap {
    try {
        Add-Content -Path (Join-Path $Pasta "abrir_dashboard.log") -Encoding UTF8 `
            -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') FALHA AO INICIAR: $($_.Exception.Message) (linha $($_.InvocationInfo.ScriptLineNumber))"
    } catch { }
    break
}
$Saude = "http://127.0.0.1:3000/api/health"
$Containers = @("swfast-postgres", "swfast-metabase")
$EsperaMaximaSegundos = 180

$Log = Join-Path $Pasta "abrir_dashboard.log"
# Requisições a 127.0.0.1 nunca passam por proxy do sistema.
[System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy

function Registrar([string]$Texto) {
    # O script roda escondido: o log é o único jeito de saber o que houve.
    try {
        New-Item -ItemType Directory -Force -Path $Pasta | Out-Null
        if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 512KB) { Remove-Item $Log -Force }
        Add-Content -Path $Log -Encoding UTF8 -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Texto"
    } catch { }
}

function Mostrar-Erro([string]$Mensagem) {
    Registrar "ERRO: $Mensagem"
    Add-Type -AssemblyName System.Windows.Forms
    # DefaultDesktopOnly: a mensagem aparece na frente de tudo. Sem isso ela
    # abria atrás das outras janelas e o script ficava esperando um OK invisível.
    [System.Windows.Forms.MessageBox]::Show(
        $Mensagem, "Dashboard Swfast", "OK", "Warning", "Button1", "DefaultDesktopOnly"
    ) | Out-Null
}

function Metabase-Responde {
    try {
        $resposta = Invoke-WebRequest -Uri $Saude -UseBasicParsing -TimeoutSec 5
        return $resposta.Content -match '"status"\s*:\s*"ok"'
    } catch {
        Registrar "Metabase ainda não respondeu: $($_.Exception.Message)"
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

# O atalho roda este script escondido, e o Windows não deixa um processo em
# segundo plano pôr uma janela na frente das outras: sem isto, o dashboard
# abria atrás da janela que estivesse em uso e parecia que nada acontecia.
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

namespace Swfast {
    public static class Janela {
        delegate bool Visitar(IntPtr janela, IntPtr extra);
        [DllImport("user32.dll")] static extern bool EnumWindows(Visitar visitar, IntPtr extra);
        [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr janela);
        [DllImport("user32.dll")] static extern int GetWindowTextLength(IntPtr janela);
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr janela, out uint pid);
        [DllImport("user32.dll")] static extern bool IsIconic(IntPtr janela);
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr janela, int modo);
        [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr janela);
        [DllImport("user32.dll")] static extern void keybd_event(byte tecla, byte scan, uint flags, UIntPtr extra);

        // A primeira janela visível, com título, de um destes processos.
        public static IntPtr Encontrar(uint[] pids) {
            var procurados = new HashSet<uint>(pids);
            IntPtr achada = IntPtr.Zero;
            EnumWindows((janela, extra) => {
                uint pid;
                GetWindowThreadProcessId(janela, out pid);
                if (procurados.Contains(pid) && IsWindowVisible(janela) && GetWindowTextLength(janela) > 0) {
                    achada = janela;
                    return false;
                }
                return true;
            }, IntPtr.Zero);
            return achada;
        }

        public static void TrazerParaFrente(IntPtr janela) {
            if (IsIconic(janela)) ShowWindow(janela, 9);  // 9 = restaurar
            ShowWindow(janela, 3);                        // 3 = maximizar
            // Um Alt simulado conta como "uso recente do teclado" e libera a troca de foco.
            keybd_event(0x12, 0, 0, UIntPtr.Zero);
            keybd_event(0x12, 0, 2, UIntPtr.Zero);
            SetForegroundWindow(janela);
        }
    }
}
'@

function Janela-Do-Dashboard {
    # Janela visível de qualquer processo do Edge com o perfil do dashboard.
    # (Process.MainWindowHandle não serve: com o Edge ele às vezes aponta para
    # uma janela interna invisível, ou para nenhuma com a janela aberta.)
    $ids = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" |
        Where-Object { $_.CommandLine -like "*$Pasta*" } |
        ForEach-Object { [uint32]$_.ProcessId })
    if ($ids.Count -eq 0) { return [IntPtr]::Zero }
    return [Swfast.Janela]::Encontrar($ids)
}

function Trazer-Para-Frente([IntPtr]$Janela) {
    [Swfast.Janela]::TrazerParaFrente($Janela)
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
    Registrar "Abrindo o dashboard."

    # Já está aberto (talvez minimizado ou atrás de outra janela): só traz para frente.
    $aberta = Janela-Do-Dashboard
    if ($aberta -ne [IntPtr]::Zero) {
        Registrar "Janela já aberta: trazendo para frente."
        Trazer-Para-Frente $aberta
        return
    }

    if (-not (Metabase-Responde)) {
        Registrar "Metabase fora do ar: ligando os containers."
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
        # Espera a janela existir (até 30s) e a põe na frente.
        $inicio = Get-Date
        while (($janela = Janela-Do-Dashboard) -eq [IntPtr]::Zero -and ((Get-Date) - $inicio).TotalSeconds -lt 30) {
            Start-Sleep -Milliseconds 500
        }
        if ($janela -ne [IntPtr]::Zero) {
            Registrar "Janela aberta."
            Trazer-Para-Frente $janela
        } else {
            Registrar "O Edge foi iniciado, mas a janela não apareceu em 30s."
        }
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
