# SafeOti

Aplicação desktop para Windows 10/11 que limpa, monitoriza e otimiza o PC. Interface em português de Portugal, com tema claro: fundo branco e detalhes a preto.

## Funcionalidades

| Página | O que faz |
| --- | --- |
| **Painel** | CPU (total, por núcleo e frequência), RAM, espaço por unidade, velocidade de leitura/escrita por disco físico, rede em tempo real. Gráficos dos últimos 60 segundos. Temperaturas só aparecem se o sistema as disponibilizar. |
| **Limpeza** | Temp do utilizador, Temp do Windows, cache de miniaturas, cache do Chrome/Edge/Firefox, cache do Windows Update e Reciclagem. "Analisar" mostra tamanho e número de ficheiros antes de apagar; "Limpar" pede confirmação e mostra o espaço libertado. |
| **Arranque** | Programas que iniciam com o Windows (chaves `Run` de HKCU/HKLM, incluindo WOW6432Node, e pastas de arranque do utilizador e comum). Ativar/desativar usa as chaves `StartupApproved`, como o Gestor de Tarefas: nada é apagado. |
| **Processos** | Nome, PID, CPU %, RAM e utilizador, atualizado a cada 2 s sem perder a seleção nem o scroll. Ordenação por coluna, pesquisa, terminar com confirmação e abrir a pasta do executável. |
| **Modo Gaming** | Guarda o plano de energia atual e muda para Desempenho máximo (se existir) ou Alto desempenho; fecha as apps da lista; opcionalmente dá prioridade Alta ao jogo. Ao desativar repõe o plano e a prioridade. |
| **Definições** | Lista de apps a fechar no Modo Gaming, intervalo de atualização do monitor, iniciar com o Windows. Guardadas em `%APPDATA%\SafeOti\settings.json`. |

### Segurança

- Deteta se está a correr como administrador. Se não estiver, as páginas que precisam mostram um aviso e o botão **Reiniciar como administrador**.
- Botão **Criar ponto de restauro** (PowerShell `Checkpoint-Computer`), com mensagens claras se o Restauro do Sistema estiver desativado ou se já existir um ponto criado nas últimas 24 horas.
- Todas as ações destrutivas pedem confirmação.
- A limpeza só apaga conteúdo das pastas de cada categoria: nunca apaga a própria pasta, não segue nem apaga symlinks, junctions ou outros reparse points e recusa pastas perigosas (raiz da unidade, pasta do utilizador, `C:\Windows`, Program Files…) mesmo que uma variável de ambiente aponte para elas. Ficheiros em uso ou sem permissão são ignorados sem interromper. A Reciclagem é esvaziada com `SHEmptyRecycleBinW`.
- Processos críticos (System, smss.exe, csrss.exe, wininit.exe, winlogon.exe, services.exe, lsass.exe, svchost.exe, entre outros) não podem ser terminados. Terminar o `explorer.exe` mostra um aviso próprio.
- O Modo Gaming só usa `powercfg` e a prioridade de processos: não aplica "tweaks" de registo. Fecha apenas processos do utilizador atual. O estado anterior fica guardado em `%APPDATA%\SafeOti\gaming_state.json` antes de qualquer alteração, por isso pode ser reposto mesmo depois de fechar a app.
- Todas as ações ficam registadas em `%APPDATA%\SafeOti\logs\safeoti.log` (rotativo, 5 × 2 MB).
- Exceções inesperadas ficam no registo e aparecem como mensagem, sem fechar a app.

## Requisitos

- Windows 10 ou 11 (64 bits)
- Python 3.11 ou superior (só para correr a partir do código ou compilar)

## Instalar e correr a partir do código

```bat
cd SafeOti
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Para ter acesso a todas as funções, abra a consola **como administrador** antes de `python main.py`, ou use o botão **Reiniciar como administrador** dentro da app.

`python main.py --minimizado` abre a janela minimizada. É o modo usado ao iniciar com o Windows.

## Compilar o executável

```bat
cd SafeOti
build.bat
```

O `build.bat`:

1. cria o ambiente virtual `.venv` (se não existir);
2. instala as dependências de `requirements.txt`;
3. executa os testes (`build.bat --sem-testes` salta este passo);
4. gera `dist\SafeOti.exe` com PyInstaller: um único ficheiro, sem consola, com o manifesto `SafeOti.manifest` (`requireAdministrator`, DPI por monitor, caminhos longos).

Ao abrir o `.exe`, o Windows pede confirmação de administrador (UAC). Alguns antivírus desconfiam de executáveis PyInstaller não assinados. Se acontecer, assine o executável com um certificado de assinatura de código ou adicione uma exceção.

## Testes

```bat
python -m unittest discover -s tests -v
```

Os testes cobrem a lógica de `core/`: segurança da limpeza (symlinks, raízes proibidas, cancelamento), formato `StartupApproved`, leitura do `powercfg` e validação das definições. Correm em qualquer sistema operativo.

## Estrutura

```
SafeOti/
├── main.py              # ponto de entrada (log, exceções, tema, janela)
├── requirements.txt
├── build.bat            # compila dist\SafeOti.exe
├── SafeOti.manifest   # manifesto do executável (pede administrador)
├── core/                # lógica, sem dependência de PySide6
│   ├── admin.py         # administrador, reinício elevado, ponto de restauro
│   ├── cleaner.py       # análise e limpeza
│   ├── gaming.py        # Modo Gaming (powercfg, apps, prioridade)
│   ├── monitor.py       # métricas do sistema
│   ├── processes.py     # lista e término seguro de processos
│   ├── settings.py      # definições JSON e log
│   ├── startup.py       # gestor de arranque e tarefa agendada da própria app
│   └── utils.py         # comandos sem consola, formatação
├── ui/
│   ├── main_window.py   # barra lateral e páginas
│   ├── common.py        # diálogos e widgets partilhados
│   ├── icons.py         # ícones de linha (SVG) e logótipo, desenhados em código
│   ├── workers.py       # QThreads (tarefas, monitor, processos)
│   ├── styles.qss       # tema claro (branco e preto)
│   ├── check.svg, arrow-up.svg, arrow-down.svg
│   └── pages/           # dashboard, cleaner_page, startup_page, processes_page, gaming_page, settings_page
└── tests/
    └── test_core.py
```

## Notas

- **Iniciar com o Windows:** como o executável pede administrador, o Windows não o arranca a partir da chave `Run`. O SafeOti cria uma tarefa agendada `SafeOti` (ao iniciar sessão, com privilégios mais elevados, 15 s de atraso). Para isso, a app tem de estar a correr como administrador.
- **Cache do Windows Update:** o serviço Windows Update não é parado. Ficheiros em uso por uma atualização em curso são ignorados.
- **Browsers:** feche-os antes de limpar a cache para libertar mais espaço.
- **Temperaturas:** o Windows não expõe sensores ao `psutil`. A app tenta a zona térmica ACPI por WMI e esconde a secção se não houver dados.
- **Plano "Alto desempenho" oculto:** em alguns portáteis com Modern Standby o plano não está disponível. Nesse caso o Modo Gaming avisa e mantém o plano atual.
