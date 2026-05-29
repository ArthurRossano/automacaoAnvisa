import os
import time
import queue
import subprocess
import threading
import re
import csv
import unicodedata
import openpyxl
from flask import Flask, render_template, jsonify, request, Response
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Carrega configurações do .env inicial
load_dotenv()

app = Flask(__name__)

# Controle de subprocessos ativos
processes = {
    1: None,
    2: None,
    3: None,
    4: None
}

# Listas de ouvintes SSE ativos (filas de mensagens)
log_queues = {
    1: [],
    2: [],
    3: [],
    4: []
}

# Histórico de logs para manter o console cheio ao recarregar
log_history = {
    1: [],
    2: [],
    3: [],
    4: []
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
    
    cmd = ["python", "main.py", "--part", str(part), "--total-parts", "4"]
    
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
        half_rows = (total_rows + 3) // 4  # tamanho estimado de cada lote (25%)
        
        return jsonify({
            "success": True,
            "total_rows": total_rows,
            "half_rows": half_rows
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/status')
def api_status():
    """Retorna se os processos da Parte 1 ao 4 estão ativos no momento."""
    return jsonify({
        "part1": {"running": processes[1] is not None},
        "part2": {"running": processes[2] is not None},
        "part3": {"running": processes[3] is not None},
        "part4": {"running": processes[4] is not None}
    })


@app.route('/api/start', methods=['POST'])
def api_start():
    """Dispara a execução do script para a metade selecionada em uma thread separada."""
    part = request.args.get('part', type=int)
    if part not in [1, 2, 3, 4]:
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
    if part not in [1, 2, 3, 4]:
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
    if part not in [1, 2, 3, 4]:
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

# Diretório temporário para uploads dentro do workspace
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'temp_uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def normalize_string(s):
    if s is None:
        return ""
    # Remove acentos e converte para minúsculas
    s = unicodedata.normalize('NFKD', str(s)).encode('ASCII', 'ignore').decode('utf-8')
    s = s.lower().strip()
    # Remove caracteres especiais e deixa apenas letras e números
    s = re.sub(r'[^a-z0-9]', '', s)
    return s

def map_columns(upload_headers, sheets_headers):
    mapping = {} # { sheets_header: upload_header_index }
    
    synonyms = {
        'REGISTRO ANVISA': ['registro', 'anvisa', 'processo', 'nro registro', 'nregistro', 'num registro', 'numero registro'],
        'Descrição do item': ['descri', 'desc', 'item', 'produto', 'nome', 'descricao do item', 'descricao'],
        'Nº do item': ['nº', 'n do item', 'nº do item', 'numero', 'nro', 'codigo', 'código', 'id', 'sku'],
        'Fabricante': ['fabricante', 'marca', 'brand', 'fabricador'],
        'Código NCM': ['ncm', 'codigo ncm', 'código ncm', 'classificacao fiscal'],
        'Grupo de itens': ['grupo', 'categoria', 'grupo de itens'],
        'SubGrupo': ['subgrupo', 'subcategoria'],
        'BU': ['bu', 'business unit', 'unidade', 'unidade de negocio', 'segmento'],
        'VALIDADE REGISTRO ANVISA': ['validade', 'vencimento', 'expira', 'validade registro'],
        'Situação': ['situa', 'status', 'ativo', 'situacao'],
        'Classe': ['classe', 'risco', 'classe de risco', 'enquadramento'],
        'Última Atualização': ['atualiz', 'data consulta', 'verificado', 'ultima atualizacao']
    }
    
    # 1. Correspondência exata normalizada
    for s_header in sheets_headers:
        norm_s = normalize_string(s_header)
        for idx, u_header in enumerate(upload_headers):
            if normalize_string(u_header) == norm_s:
                mapping[s_header] = idx
                break
                
    # 2. Correspondência aproximada usando sinônimos
    for s_header in sheets_headers:
        if s_header in mapping:
            continue
            
        norm_s = normalize_string(s_header)
        
        # Procura qual chave do sinônimo combina com o cabeçalho do Sheets
        key_match = None
        for key in synonyms:
            norm_key = normalize_string(key)
            if norm_key in norm_s or norm_s in norm_key:
                key_match = key
                break
                
        if key_match:
            for idx, u_header in enumerate(upload_headers):
                norm_u = normalize_string(u_header)
                # Verifica se a palavra do upload bate com algum sinônimo
                if any(syn in norm_u or norm_u in syn for syn in synonyms[key_match]):
                    if idx not in mapping.values():
                        mapping[s_header] = idx
                        break
                        
    return mapping

def read_csv(file_path):
    encodings = ['utf-8', 'latin1', 'iso-8859-1', 'utf-16']
    for enc in encodings:
        try:
            with open(file_path, mode='r', encoding=enc) as f:
                content = f.read(2048)
                f.seek(0)
                
                # Detecta delimitador comum no Brasil
                delimiter = ';'
                if ',' in content and content.count(',') > content.count(';'):
                    delimiter = ','
                elif '\t' in content:
                    delimiter = '\t'
                    
                reader = csv.reader(f, delimiter=delimiter)
                rows = list(reader)
                if rows:
                    return rows
        except Exception:
            continue
    raise ValueError("Não foi possível decodificar o arquivo CSV. Verifique a codificação.")

def read_xlsx(file_path):
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    sheet = wb.active
    rows = []
    for row in sheet.iter_rows(values_only=True):
        if any(cell is not None for cell in row):
            rows.append(list(row))
    return rows

@app.route('/api/upload', methods=['POST'])
def api_upload():
    """Recebe o arquivo de planilha, processa, remove duplicados e insere novos itens no Google Sheets."""
    if 'file' not in request.files:
        return jsonify({"success": False, "message": "Nenhum arquivo enviado."})
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "message": "Nenhum arquivo selecionado."})
        
    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()
    
    if ext not in ['.xlsx', '.csv']:
        return jsonify({"success": False, "message": "Formato inválido. Envie arquivos .xlsx ou .csv."})
        
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    
    try:
        file.save(file_path)
        
        # 1. Lê a planilha de upload
        if ext == '.csv':
            rows = read_csv(file_path)
        else:
            rows = read_xlsx(file_path)
            
        if len(rows) < 2:
            return jsonify({"success": False, "message": "A planilha de upload está vazia ou sem dados de produtos."})
            
        upload_headers = [str(h).strip() for h in rows[0]]
        upload_data = rows[1:]
        
        # 2. Conecta ao Google Sheets
        load_dotenv()
        sheet_name = os.environ.get("SPREADSHEET_NAME")
        tab_name = os.environ.get("SHEET_TAB_NAME", "Sheet1")
        
        if not sheet_name:
            return jsonify({"success": False, "message": "Planilha Google Sheets não configurada nas configurações (.env)."})
            
        from main import get_google_sheets_client, clean_registro
        
        client = get_google_sheets_client()
        sheet = client.open(sheet_name).worksheet(tab_name)
        
        sheets_headers = sheet.row_values(1)
        
        # 3. Mapeia colunas de upload para as colunas do Sheets
        col_map = map_columns(upload_headers, sheets_headers)
        
        # Identifica a coluna do Número do Item (Coluna A do Sheets)
        col_item_idx = None
        sheets_item_header = None
        for idx, header in enumerate(sheets_headers, start=1):
            header_lower = header.lower().strip()
            if any(term in header_lower for term in ["nº do item", "n do item", "nº", "nro", "código", "codigo", "sku", "item"]):
                if "registro" not in header_lower and "descri" not in header_lower and "grupo" not in header_lower:
                    if col_item_idx is None:
                        col_item_idx = idx
                        sheets_item_header = header
                        break
                        
        # Fallback para a primeira coluna (coluna A) caso falhe a busca automática
        if col_item_idx is None:
            col_item_idx = 1
            sheets_item_header = sheets_headers[0]
            
        if sheets_item_header not in col_map:
            col_map[sheets_item_header] = 0
            
        # 4. Obtém os registros existentes da coluna de itens do Google Sheets
        existing_items_raw = sheet.col_values(col_item_idx)
        
        # Função auxiliar de limpeza para comparação case-insensitive
        def clean_item_code(val):
            if val is None:
                return ""
            return str(val).strip().lower()
            
        existing_clean_set = {clean_item_code(r) for r in existing_items_raw[1:] if r}
        
        upload_item_idx = col_map[sheets_item_header]
        
        rows_to_insert = []
        duplicados_count = 0
        vazios_count = 0
        
        for row_data in upload_data:
            # Previne linhas mais curtas que o esperado
            if len(row_data) <= upload_item_idx:
                vazios_count += 1
                continue
                
            item_original = str(row_data[upload_item_idx]).strip()
            item_limpo = clean_item_code(item_original)
            
            if not item_limpo:
                vazios_count += 1
                continue
                
            if item_limpo in existing_clean_set:
                duplicados_count += 1
                continue
                
            # É um item novo! Mapeia os dados para a estrutura do Sheets
            new_row = []
            for s_header in sheets_headers:
                upload_col_idx = col_map.get(s_header)
                if upload_col_idx is not None and upload_col_idx < len(row_data):
                    val = row_data[upload_col_idx]
                    # Garante que None seja salvo como string vazia no Sheets
                    if val is None:
                        val = ""
                else:
                    val = ""
                new_row.append(val)
                
            rows_to_insert.append(new_row)
            # Evita adicionar duplicados na mesma planilha de upload se houver múltiplos itens idênticos no próprio arquivo
            existing_clean_set.add(item_limpo)
            
        # 5. Insere os novos dados se houver
        if rows_to_insert:
            sheet.append_rows(rows_to_insert, value_input_option='USER_ENTERED')
            
        # 6. Remove arquivo temporário de forma segura
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
            
        return jsonify({
            "success": True,
            "total_analisados": len(upload_data),
            "novos_adicionados": len(rows_to_insert),
            "duplicados_ignorados": duplicados_count,
            "vazios_ignorados": vazios_count,
            "message": f"Processamento concluído: {len(rows_to_insert)} novos itens adicionados. {duplicados_count} duplicados e {vazios_count} linhas em branco ignorados."
        })
        
    except Exception as e:
        # Tenta remover o arquivo temporário caso ocorra erro
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        return jsonify({"success": False, "message": f"Erro interno ao processar a planilha: {str(e)}"})


if __name__ == '__main__':
    print("Iniciando Painel de Controle local da Automação ANVISA...")
    print("Acesse em seu navegador: http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
