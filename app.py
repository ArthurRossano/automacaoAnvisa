import os
import time
import queue
import subprocess
import threading
from flask import Flask, render_template, jsonify, request, Response
from dotenv import load_dotenv

# Carrega configurações do .env inicial
load_dotenv()

app = Flask(__name__)

# Controle de subprocessos ativos
processes = {
    1: None,
    2: None
}

# Listas de ouvintes SSE ativos (filas de mensagens)
log_queues = {
    1: [],
    2: []
}

# Histórico de logs para manter o console cheio ao recarregar
log_history = {
    1: [],
    2: []
}

def update_dotenv(key, value):
    """Atualiza de forma segura uma variável de ambiente no arquivo .env local."""
    dotenv_path = ".env"
    if not os.path.exists(dotenv_path):
        with open(dotenv_path, "w", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
        return

    with open(dotenv_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    updated = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            updated = True
            break

    if not updated:
        lines.append(f"{key}={value}\n")

    with open(dotenv_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def run_process_thread(part):
    """Thread em segundo plano que executa o script da automação e captura o stdout/stderr em tempo real."""
    global processes, log_history, log_queues
    
    cmd = ["python", "main.py", "--part", str(part)]
    
    # Força a saída padrão do Python a usar UTF-8 para evitar caracteres corrompidos no console do Windows
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="ignore",
            env=env
        )
        processes[part] = proc
        log_history[part] = []
        
        # Lê a saída padrão linha a linha em tempo real
        for line in iter(proc.stdout.readline, ""):
            line_clean = line.strip()
            if not line_clean:
                continue
            
            log_history[part].append(line_clean)
            
            # Distribui o log para todos os ouvintes do SSE
            for q in list(log_queues[part]):
                q.put(line_clean)
                
        proc.stdout.close()
        return_code = proc.wait()
        
    except Exception as e:
        error_msg = f"[ERROR] Falha crítica no subprocesso: {e}"
        log_history[part].append(error_msg)
        for q in list(log_queues[part]):
            q.put(error_msg)
        return_code = -1
    finally:
        processes[part] = None
        
        # Envia sinalizador de finalização aos ouvintes do SSE
        for q in list(log_queues[part]):
            q.put(f"__FINISHED__:{return_code}")


@app.route('/')
def index():
    """Rota principal do Painel."""
    return render_template('index.html')


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """Lê ou atualiza as configurações do .env."""
    if request.method == 'POST':
        data = request.json
        if not data:
            return jsonify({"success": False, "message": "Dados inválidos."})
        
        try:
            spreadsheet_name = data.get('spreadsheet_name', '').strip()
            sheet_tab_name = data.get('sheet_tab_name', '').strip()
            
            update_dotenv("SPREADSHEET_NAME", spreadsheet_name)
            update_dotenv("SHEET_TAB_NAME", sheet_tab_name)
            
            # Recarrega no contexto do Python atual
            os.environ["SPREADSHEET_NAME"] = spreadsheet_name
            os.environ["SHEET_TAB_NAME"] = sheet_tab_name
            
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})
            
    else:
        # GET
        load_dotenv()
        return jsonify({
            "spreadsheet_name": os.environ.get("SPREADSHEET_NAME", ""),
            "sheet_tab_name": os.environ.get("SHEET_TAB_NAME", "Sheet1")
        })


@app.route('/api/stats')
def api_stats():
    """Consulta o total de registros na planilha no momento da chamada."""
    load_dotenv()
    sheet_name = os.environ.get("SPREADSHEET_NAME")
    tab_name = os.environ.get("SHEET_TAB_NAME", "Sheet1")
    
    if not sheet_name:
        return jsonify({"success": False, "message": "Planilha não configurada no .env"})
        
    try:
        from main import get_google_sheets_client
        client = get_google_sheets_client()
        sheet = client.open(sheet_name).worksheet(tab_name)
        records = sheet.get_all_records()
        total_rows = len(records)
        half_rows = total_rows // 2
        
        return jsonify({
            "success": True,
            "total_rows": total_rows,
            "half_rows": half_rows
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/status')
def api_status():
    """Retorna se os processos da Parte 1 e Parte 2 estão ativos no momento."""
    return jsonify({
        "part1": {"running": processes[1] is not None},
        "part2": {"running": processes[2] is not None}
    })


@app.route('/api/start', methods=['POST'])
def api_start():
    """Dispara a execução do script para a metade selecionada em uma thread separada."""
    part = request.args.get('part', type=int)
    if part not in [1, 2]:
        return jsonify({"success": False, "message": "Parte do processo inválida."})
        
    if processes[part] is not None:
        return jsonify({"success": False, "message": f"Parte {part} já está em execução."})
        
    # Inicializa a thread de execução do subprocesso
    t = threading.Thread(target=run_process_thread, args=(part,))
    t.daemon = True
    t.start()
    
    return jsonify({"success": True})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Interrompe e mata o subprocesso ativo da metade correspondente."""
    part = request.args.get('part', type=int)
    if part not in [1, 2]:
        return jsonify({"success": False, "message": "Parte do processo inválida."})
        
    proc = processes[part]
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception as e:
                return jsonify({"success": False, "message": f"Falha ao forçar parada: {e}"})
        
        processes[part] = None
        return jsonify({"success": True})
        
    return jsonify({"success": False, "message": "O processo já está inativo."})


@app.route('/api/logs/<int:part>')
def api_logs(part):
    """Retorna o fluxo de eventos de log via SSE (Server-Sent Events) para o console do frontend."""
    if part not in [1, 2]:
        return "Parte inválida", 400

    def event_stream():
        q = queue.Queue()
        log_queues[part].append(q)
        
        # 1. Envia os logs históricos existentes para restaurar a tela
        for line in list(log_history[part]):
            yield f"event: log\ndata: {line}\n\n"
            
        # 2. Aguarda novos logs e envia em tempo real
        try:
            while True:
                line = q.get()
                if line.startswith("__FINISHED__:"):
                    code = line.split(":")[1]
                    success = "true" if code == "0" else "false"
                    yield f"event: finished\ndata: {{\"success\": {success}, \"code\": {code}}}\n\n"
                    break
                else:
                    yield f"event: log\ndata: {line}\n\n"
        except GeneratorExit:
            pass
        finally:
            if q in log_queues[part]:
                log_queues[part].remove(q)

    return Response(event_stream(), mimetype="text/event-stream")


if __name__ == '__main__':
    print("Iniciando Painel de Controle local da Automação ANVISA...")
    print("Acesse em seu navegador: http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
