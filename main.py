import os
import re
import time
import json
import socket
import logging
from datetime import datetime
from dotenv import load_dotenv

# Define timeout global para conexões de rede (evita travamentos infinitos na API do Sheets)
socket.setdefaulttimeout(30)

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

# Carrega variáveis de ambiente do arquivo .env
load_dotenv()

# Configuração de Logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Configurações do Google Sheets
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME")
SHEET_TAB_NAME = os.environ.get("SHEET_TAB_NAME", "Sheet1")

def clean_registro(reg_str):
    """Remove caracteres não numéricos do registro ANVISA."""
    if not reg_str:
        return ""
    return re.sub(r"\D", "", str(reg_str))

def safe_update_cell(sheet, row, col, value, retries=3, delay=3):
    """
    Atualiza uma célula no Google Sheets com tratamento de erros e retentativas automáticas.
    Garante que falhas temporárias de rede não interrompam o script.
    """
    for i in range(retries):
        try:
            sheet.update_cell(row, col, value)
            return True
        except Exception as e:
            logger.warning(
                f"Erro ao atualizar célula ({row}, {col}) com '{value}': {e}. "
                f"Tentando novamente {i+1}/{retries} em {delay}s..."
            )
            time.sleep(delay)
    logger.error(f"Falha definitiva ao atualizar célula ({row}, {col}) após {retries} tentativas.")
    return False

def get_google_sheets_client():
    """Autentica e retorna o cliente do Google Sheets."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    # 1. Tenta carregar dos segredos do ambiente
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        try:
            info = json.loads(creds_json)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            logger.info("Autenticado via GOOGLE_CREDENTIALS da variável de ambiente.")
            return gspread.authorize(creds)
        except Exception as e:
            logger.error(f"Erro ao ler GOOGLE_CREDENTIALS: {e}")
            
    # 2. Tenta carregar do arquivo credentials.json local
    creds_file = "credentials.json"
    if os.path.exists(creds_file):
        try:
            creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
            logger.info("Autenticado via arquivo credentials.json local.")
            return gspread.authorize(creds)
        except Exception as e:
            logger.error(f"Erro ao ler credentials.json: {e}")
            
    raise FileNotFoundError(
        "Nenhuma credencial do Google encontrada. Defina a variável GOOGLE_CREDENTIALS "
        "ou crie o arquivo credentials.json na raiz do projeto."
    )

def search_registry_visual(page, registro_limpo):
    """
    Executa a pesquisa visual no portal da ANVISA.
    Preenche o input, clica em buscar e lê o resultado da tabela.
    """
    try:
        # 1. Digita o registro no input do AngularJS
        logger.info(f"Digitando registro '{registro_limpo}'...")
        page.fill('input[ng-model="filter.numeroRegistro"]', registro_limpo, timeout=5000)
        
        # 2. Clica no botão de consulta
        logger.info("Clicando em Consultar...")
        page.click('input.btn-primary', timeout=5000)
        
        # 3. Aguarda a tabela de resultados ou mensagem de "Nenhum registro"
        start_time = time.time()
        found_data = False
        no_records = False
        
        while time.time() - start_time < 10:
            # Verifica se a tabela com linhas de dados (td) apareceu
            if page.query_selector("table tbody tr td"):
                found_data = True
                break
            # Verifica se o aviso de "não foram encontrados registros" está na página
            page_content = page.content()
            if "Não foram encontrados registros" in page_content or "não foram encontrados" in page_content.lower():
                no_records = True
                break
            time.sleep(0.5)
            
        if no_records:
            logger.info(f"Registro '{registro_limpo}' não foi encontrado no portal.")
            return None, "Não Encontrado"
            
        if not found_data:
            logger.warning(f"Timeout aguardando resultados para o registro '{registro_limpo}'.")
            return None, "Timeout/Erro"
            
        # 4. Extrai as linhas da tabela
        rows = page.query_selector_all("table tbody tr")
        for row in rows:
            cells = row.query_selector_all("td")
            if len(cells) >= 8:
                registro_tabela = clean_registro(cells[2].inner_text().strip())
                
                # Double-check para confirmar que a linha corresponde ao registro buscado
                if registro_tabela == registro_limpo:
                    situacao = cells[5].inner_text().strip()
                    validade = cells[7].inner_text().strip()
                    
                    logger.info(f"Registro '{registro_limpo}' localizado! Situação: {situacao}, Validade: {validade}")
                    return validade, situacao
                    
        logger.warning(f"Tabela de resultados carregada, mas não continha correspondência para o registro '{registro_limpo}'.")
        return None, "Não Encontrado"
        
    except Exception as e:
        logger.error(f"Erro ao realizar busca do registro '{registro_limpo}': {e}")
        return None, "Erro na Busca"

def main():
    if not SPREADSHEET_NAME:
        logger.error("Erro: A variável de ambiente SPREADSHEET_NAME não foi configurada.")
        return

    # 1. Conecta ao Google Sheets
    try:
        client = get_google_sheets_client()
        sheet = client.open(SPREADSHEET_NAME).worksheet(SHEET_TAB_NAME)
        logger.info(f"Planilha '{SPREADSHEET_NAME}' (aba '{SHEET_TAB_NAME}') aberta com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao conectar ao Google Sheets: {e}")
        return

    # 2. Lê todas as linhas da planilha
    records = sheet.get_all_records()
    if not records:
        logger.warning("A planilha está vazia ou não possui cabeçalhos válidos.")
        return
        
    headers = sheet.row_values(1)
    
    # 3. Mapeia dinamicamente os índices das colunas de interesse
    col_registro_idx = None
    col_validade_idx = None
    col_situacao_idx = None
    col_atualizacao_idx = None

    for idx, header in enumerate(headers, start=1):
        header_lower = header.lower().strip()
        
        # 1. Validade: prioridade para termos que indiquem validade/vencimento/expiração
        if any(term in header_lower for term in ["validade", "vencimento", "expira"]):
            if col_validade_idx is None:
                col_validade_idx = idx
        
        # 2. Registro ANVISA: termos de registro/processo/anvisa (não deve conter validade no nome)
        elif any(term in header_lower for term in ["registro", "anvisa", "processo"]):
            if col_registro_idx is None:
                col_registro_idx = idx
                
        # 3. Situação (opcional)
        elif any(term in header_lower for term in ["situação", "situacao", "status", "ativo"]):
            if col_situacao_idx is None:
                col_situacao_idx = idx
                
        # 4. Última Atualização (opcional)
        elif any(term in header_lower for term in ["atualização", "atualizacao", "data consulta", "verificado"]):
            if col_atualizacao_idx is None:
                col_atualizacao_idx = idx

    if col_registro_idx is None:
        logger.error("Não foi possível localizar a coluna de 'Registro ANVISA' na planilha.")
        return
    if col_validade_idx is None:
        logger.error("Não foi possível localizar a coluna de 'Validade' na planilha.")
        return

    logger.info(f"Mapeamento de colunas -> Registro: Col {col_registro_idx}, Validade: Col {col_validade_idx}, "
                f"Situação: Col {col_situacao_idx or 'N/A'}, Atualização: Col {col_atualizacao_idx or 'N/A'}")

    # 4. Inicializa o Playwright e abre o portal de Saúde diretamente
    with sync_playwright() as p:
        logger.info("Iniciando navegador Playwright (Chromium)...")
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900}
        )
        
        page = context.new_page()
        
        # 5. Itera sobre cada produto na planilha
        # O gspread usa indexação baseada em 1. A linha 1 é o cabeçalho, então os dados iniciam na linha 2.
        for idx, row in enumerate(records, start=2):
            registro_original = str(row.get(headers[col_registro_idx - 1], "")).strip()
            registro_limpo = clean_registro(registro_original)
            
            if not registro_limpo:
                logger.info(f"Linha {idx}: Registro em branco. Pulando...")
                continue

            logger.info(f"Processando linha {idx}: Registro '{registro_original}'...")
            
            # Recarrega a página antes de fazer a busca para garantir um estado limpo
            try:
                logger.info("Carregando o portal de consultas da ANVISA...")
                page.goto("https://consultas.anvisa.gov.br/#/saude/", wait_until="domcontentloaded", timeout=45000)
                # Dá um tempo para que os elementos do AngularJS terminem a inicialização
                time.sleep(3.5)
            except Exception as e:
                logger.warning(f"Erro ao carregar o portal na primeira tentativa: {e}. Tentando recarregar...")
                try:
                    page.goto("https://consultas.anvisa.gov.br/#/saude/", wait_until="domcontentloaded", timeout=45000)
                    time.sleep(5)
                except Exception as e2:
                    logger.error(f"Não foi possível acessar a ANVISA após duas tentativas: {e2}")
                    safe_update_cell(sheet, idx, col_validade_idx, "Erro de Conexão")
                    continue
            
            # Faz a busca visual no portal
            validade, situacao = search_registry_visual(page, registro_limpo)
            
            # Atualiza os valores correspondentes de volta no Google Sheets
            if validade:
                safe_update_cell(sheet, idx, col_validade_idx, validade)
                if col_situacao_idx:
                    safe_update_cell(sheet, idx, col_situacao_idx, situacao)
            else:
                safe_update_cell(sheet, idx, col_validade_idx, "Não Encontrado")
                if col_situacao_idx:
                    safe_update_cell(sheet, idx, col_situacao_idx, "Inativo ou Não Encontrado")
            
            if col_atualizacao_idx:
                data_hoje = datetime.now().strftime("%d/%m/%Y %H:%M")
                safe_update_cell(sheet, idx, col_atualizacao_idx, data_hoje)
                
            # Rate limiting amigável para evitar bloqueio por flood de IP
            time.sleep(2)
            
        browser.close()
        logger.info("Automação concluída com sucesso!")

if __name__ == "__main__":
    main()
