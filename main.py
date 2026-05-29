import os
import re
import time
import json
import socket
import logging
import argparse
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

def commit_batch(sheet, cells_list):
    """
    Grava as células acumuladas no Google Sheets em uma única requisição (Batch Update).
    Limpa a lista de células após a gravação com sucesso.
    """
    if not cells_list:
        return True
    
    logger.info(f"Gravando lote de {len(cells_list)} atualizações no Google Sheets...")
    for i in range(3):
        try:
            sheet.update_cells(cells_list)
            cells_list.clear()  # Limpa o lote após salvar com sucesso
            logger.info("Lote de atualizações gravado com sucesso!")
            return True
        except Exception as e:
            logger.warning(f"Erro ao gravar lote: {e}. Tentando novamente {i+1}/3 em 3s...")
            time.sleep(3)
    logger.error("Falha definitiva ao gravar o lote no Google Sheets.")
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
    Preenche o input, clica em buscar e lê o resultado da tabela,
    e em seguida acessa os detalhes do produto para obter a Classe de Risco.
    Se o registro não for encontrado na primeira tentativa, faz mais uma tentativa
    limpando o estado da página completamente.
    """
    max_tentativas = 2
    for tentativa in range(1, max_tentativas + 1):
        try:
            logger.info(f"Iniciando busca do registro '{registro_limpo}' (Tentativa {tentativa}/{max_tentativas})...")
            
            # Se for a segunda tentativa, força o recarregamento completo da página
            if tentativa > 1:
                logger.info(f"Forçando recarregamento completo para nova verificação do registro '{registro_limpo}'...")
                page.goto("https://consultas.anvisa.gov.br/#/saude/", wait_until="domcontentloaded", timeout=45000)
                time.sleep(3)
            else:
                # Na primeira tentativa, tenta limpar os filtros anteriores
                try:
                    page.click('input.btn-default', timeout=2000)
                    time.sleep(0.3)
                except Exception:
                    logger.warning("Página travada ou botão Limpar indisponível. Recarregando portal...")
                    page.goto("https://consultas.anvisa.gov.br/#/saude/", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(3)
            
            # 2. Aguarda o input estar disponível dinamicamente
            page.wait_for_selector('input[ng-model="filter.numeroRegistro"]', state="visible", timeout=5000)
            
            # 3. Digita o registro no input do AngularJS
            logger.info(f"Digitando registro '{registro_limpo}'...")
            page.fill('input[ng-model="filter.numeroRegistro"]', registro_limpo, timeout=3000)
            
            # 4. Clica no botão de consulta
            logger.info("Clicando em Consultar...")
            page.click('input.btn-primary', timeout=3000)
            
            # 5. Aguarda a tabela de resultados ou mensagem de "Nenhum registro"
            start_time = time.time()
            found_data = False
            no_records = False
            
            while time.time() - start_time < 8:
                # Verifica se a tabela com linhas de dados (td) apareceu
                if page.query_selector("table tbody tr td"):
                    found_data = True
                    break
                # Verifica se o aviso de "não foram encontrados registros" está na página
                page_content = page.content()
                if "Não foram encontrados registros" in page_content or "não foram encontrados" in page_content.lower():
                    no_records = True
                    break
                time.sleep(0.3)
                
            if no_records:
                if tentativa < max_tentativas:
                    logger.warning(f"Registro '{registro_limpo}' retornou 'Não Encontrado' na primeira tentativa. Tentando mais uma vez...")
                    continue
                else:
                    logger.info(f"Registro '{registro_limpo}' não foi encontrado no portal após {max_tentativas} tentativas.")
                    return None, "Não Encontrado", "Não Encontrada"
                
            if not found_data:
                if tentativa < max_tentativas:
                    logger.warning(f"Timeout aguardando resultados para o registro '{registro_limpo}' na primeira tentativa. Tentando mais uma vez...")
                    continue
                else:
                    logger.warning(f"Timeout aguardando resultados para o registro '{registro_limpo}' após {max_tentativas} tentativas.")
                    return None, "Timeout/Erro", "Não Encontrada"
                
            # 6. Extrai as linhas da tabela e acessa os detalhes do produto correspondente
            rows = page.query_selector_all("table tbody tr")
            for row in rows:
                cells = row.query_selector_all("td")
                if len(cells) >= 8:
                    registro_tabela = clean_registro(cells[2].inner_text().strip())
                    
                    # Double-check para confirmar que a linha corresponde ao registro buscado
                    if registro_tabela == registro_limpo:
                        situacao = cells[5].inner_text().strip()
                        validade = cells[7].inner_text().strip()
                        
                        logger.info(f"Registro '{registro_limpo}' localizado! Situação: {situacao}, Validade: {validade}. Acessando detalhes para obter Classe de Risco...")
                        
                        classe_risco = "Não Encontrada"
                        try:
                            # Clica no segundo TD (Nome do Produto) que contém o trigger de clique para os detalhes
                            cells[1].click()
                            
                            # Aguarda o elemento da Classificação de Risco na página de detalhes
                            page.wait_for_selector('xpath=//th[contains(text(), "Classificação de Risco")]', timeout=6000)
                            
                            # Loop de espera ativa para o AngularJS carregar o valor real no TD (que inicialmente pode ser "-" ou vazio)
                            start_wait = time.time()
                            while time.time() - start_wait < 5:
                                val = page.locator('xpath=//th[contains(text(), "Classificação de Risco")]/following-sibling::td').first.inner_text().strip()
                                if val and val != "-":
                                    classe_risco = val
                                    break
                                time.sleep(0.2)
                                
                            logger.info(f"Classe de Risco extraída: '{classe_risco}'")
                        except Exception as e_detail:
                            logger.error(f"Erro ao acessar/extrair detalhes do registro '{registro_limpo}': {e_detail}")
                            
                        # Recarrega diretamente o portal de consultas para garantir um estado limpo
                        logger.info("Recarregando portal de consultas para a próxima busca...")
                        try:
                            page.goto("https://consultas.anvisa.gov.br/#/saude/", wait_until="domcontentloaded", timeout=20000)
                            time.sleep(2)
                        except Exception as e_goto:
                            logger.warning(f"Erro ao recarregar o portal: {e_goto}. Tentando recarregar novamente...")
                            page.goto("https://consultas.anvisa.gov.br/#/saude/", wait_until="domcontentloaded", timeout=30000)
                            time.sleep(3)
                            
                        return validade, situacao, classe_risco
            
            # Se a tabela carregou mas não continha correspondência
            if tentativa < max_tentativas:
                logger.warning(f"Tabela de resultados carregada para '{registro_limpo}', mas sem correspondência na primeira tentativa. Tentando mais uma vez...")
                continue
            else:
                logger.warning(f"Tabela de resultados carregada, mas não continha correspondência para o registro '{registro_limpo}' após {max_tentativas} tentativas.")
                return None, "Não Encontrado", "Não Encontrada"
                
        except Exception as e:
            logger.error(f"Erro ao realizar busca do registro '{registro_limpo}' na tentativa {tentativa}: {e}")
            if tentativa < max_tentativas:
                continue
            else:
                return None, "Erro na Busca", "Não Encontrada"

def main():
    # Configura os argumentos de linha de comando para paralelismo dinâmico
    parser = argparse.ArgumentParser(description="Automação ANVISA com paralelização dinâmica.")
    parser.add_argument("--part", type=int, help="Índice da parte a ser processada (1 a total-parts)")
    parser.add_argument("--total-parts", type=int, default=4, help="Número total de divisões da planilha (padrão: 4)")
    args = parser.parse_args()

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

    # Cria lista de tarefas mapeando para o índice físico das linhas no Google Sheets (linha 1 é cabeçalho, dados iniciam na 2)
    all_tasks = list(enumerate(records, start=2))
    
    # Divisão em lotes e agrupamento serão feitos após o mapeamento das colunas.

    # 3. Mapeia dinamicamente os índices das colunas de interesse
    col_registro_idx = None
    col_validade_idx = None
    col_situacao_idx = None
    col_atualizacao_idx = None
    col_classe_idx = None

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
        elif any(term in header_lower for term in ["atualiz", "data consulta", "verificado"]):
            if col_atualizacao_idx is None:
                col_atualizacao_idx = idx
                
        # 5. Classe de Risco (opcional)
        elif any(term in header_lower for term in ["classe", "risco", "enquadramento"]):
            if col_classe_idx is None:
                col_classe_idx = idx

    if col_registro_idx is None:
        logger.error("Não foi possível localizar a coluna de 'Registro ANVISA' na planilha.")
        return
    if col_validade_idx is None:
        logger.error("Não foi possível localizar a coluna de 'Validade' na planilha.")
        return

    logger.info(f"Mapeamento de colunas -> Registro: Col {col_registro_idx}, Validade: Col {col_validade_idx}, "
                f"Situação: Col {col_situacao_idx or 'N/A'}, Atualização: Col {col_atualizacao_idx or 'N/A'}, "
                f"Classe: Col {col_classe_idx or 'N/A'}")

    # 3.5 Agrupamento por registro para evitar buscas duplicadas
    grouped_tasks = {}
    no_reg_tasks = []
    
    for idx, row in all_tasks:
        registro_original = str(row.get(headers[col_registro_idx - 1], "")).strip()
        registro_limpo = clean_registro(registro_original)
        if not registro_limpo:
            no_reg_tasks.append((idx, row))
        else:
            if registro_limpo not in grouped_tasks:
                grouped_tasks[registro_limpo] = []
            grouped_tasks[registro_limpo].append((idx, row))
            
    unique_regs = sorted(list(grouped_tasks.keys()))
    
    # Se o argumento --part for informado, divide as tarefas dinamicamente de acordo com o total de partes
    if args.part:
        total_parts = args.total_parts
        part_idx = args.part
        
        if part_idx < 1 or part_idx > total_parts:
            logger.error(f"Erro: O índice da parte ({part_idx}) deve ser entre 1 e total-parts ({total_parts}).")
            return
            
        total_unique = len(unique_regs)
        # Calcula o tamanho de cada fatia (divisão inteira com teto)
        chunk_size = (total_unique + total_parts - 1) // total_parts
        
        start_idx = (part_idx - 1) * chunk_size
        end_idx = min(start_idx + chunk_size, total_unique)
        
        part_regs = unique_regs[start_idx:end_idx]
        
        tasks_to_process = []
        for reg in part_regs:
            tasks_to_process.extend(grouped_tasks[reg])
            
        # Também divide tarefas sem registro entre as partes
        total_no_reg = len(no_reg_tasks)
        if total_no_reg > 0:
            no_reg_chunk = (total_no_reg + total_parts - 1) // total_parts
            nr_start = (part_idx - 1) * no_reg_chunk
            nr_end = min(nr_start + no_reg_chunk, total_no_reg)
            tasks_to_process.extend(no_reg_tasks[nr_start:nr_end])
            
        # Ordena as tarefas por índice de linha física para manter a progressão visual ascendente
        tasks_to_process.sort(key=lambda t: t[0])
        
        if tasks_to_process:
            first_line = tasks_to_process[0][0]
            last_line = tasks_to_process[-1][0]
            logger.info(
                f"Modo Paralelo (Parte {part_idx}/{total_parts}) ativo. "
                f"Processando {len(tasks_to_process)} registros (de registros únicos agrupados, linhas de {first_line} até {last_line})."
            )
        else:
            logger.info(f"Modo Paralelo (Parte {part_idx}/{total_parts}) ativo. Nenhuma tarefa para processar neste lote.")
    else:
        tasks_to_process = all_tasks
        logger.info(f"Modo Padrão ativo. Processando todas as {len(tasks_to_process)} linhas da planilha.")

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
        
        logger.info("Acessando aba de Saúde da ANVISA pela primeira vez...")
        try:
            page.goto("https://consultas.anvisa.gov.br/#/saude/", wait_until="domcontentloaded", timeout=45000)
            # Dá um tempo para que os elementos do AngularJS terminem a inicialização na primeira carga
            time.sleep(4)
            logger.info("Portal de Saúde inicializado com sucesso.")
        except Exception as e:
            logger.error(f"Erro crítico ao abrir o portal de consultas da ANVISA: {e}")
            browser.close()
            return

        # Lista para armazenar as células que serão atualizadas em lote
        cells_to_update = []
        batch_size = 20  # Grava no Google Sheets em lotes de 20 registros
        
        # Cache em memória para evitar buscas duplicadas no mesmo processo nesta rodada
        cache_consultas = {}

        # 5. Itera sobre cada produto fatiado na planilha
        total_tasks = len(tasks_to_process)
        for i, (idx, row) in enumerate(tasks_to_process):
            # Log de progresso para atualização da interface do dashboard
            logger.info(f"[PROGRESS] {i+1}/{total_tasks}")
            
            registro_original = str(row.get(headers[col_registro_idx - 1], "")).strip()
            registro_limpo = clean_registro(registro_original)
            
            if not registro_limpo:
                logger.info(f"Linha {idx}: Registro em branco. Pulando...")
                continue

            # Se já consultamos este registro nesta rodada, recupera os dados salvos em cache
            if registro_limpo in cache_consultas:
                validade, situacao, classe, consultado = cache_consultas[registro_limpo]
                
                # Se o registro foi apenas pulado por ter sido atualizado recentemente,
                # nós só pulamos as duplicadas silenciosamente se a linha atual já possuir a Validade preenchida na planilha.
                val_atual = str(row.get(headers[col_validade_idx - 1], "")).strip()
                if not consultado and val_atual:
                    logger.info(f"Linha {idx}: Registro '{registro_original}' já atualizado recentemente. Pulando duplicada...")
                    continue
                    
                logger.info(f"Linha {idx}: Registro '{registro_original}' recuperado do cache local desta rodada. Pulando busca visual...")
                
                if validade:
                    cells_to_update.append(gspread.cell.Cell(row=idx, col=col_validade_idx, value=validade))
                    if col_situacao_idx:
                        cells_to_update.append(gspread.cell.Cell(row=idx, col=col_situacao_idx, value=situacao))
                    if col_classe_idx:
                        cells_to_update.append(gspread.cell.Cell(row=idx, col=col_classe_idx, value=classe))
                else:
                    cells_to_update.append(gspread.cell.Cell(row=idx, col=col_validade_idx, value="Não Encontrado"))
                    if col_situacao_idx:
                        cells_to_update.append(gspread.cell.Cell(row=idx, col=col_situacao_idx, value="Inativo ou Não Encontrado"))
                    if col_classe_idx:
                        cells_to_update.append(gspread.cell.Cell(row=idx, col=col_classe_idx, value="Não Encontrada"))
                
                if col_atualizacao_idx:
                    data_hoje = datetime.now().strftime("%d/%m/%Y %H:%M")
                    cells_to_update.append(gspread.cell.Cell(row=idx, col=col_atualizacao_idx, value=data_hoje))
                
                if len(cells_to_update) >= (batch_size * 2):
                    commit_batch(sheet, cells_to_update)
                continue

            # Verifica a última atualização se a coluna existir
            if col_atualizacao_idx:
                ult_atualizacao_str = str(row.get(headers[col_atualizacao_idx - 1], "")).strip()
                if ult_atualizacao_str:
                    try:
                        dt_atualizacao = None
                        # Tenta parsear no formato com hora ou apenas data
                        for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
                            try:
                                dt_atualizacao = datetime.strptime(ult_atualizacao_str, fmt)
                                break
                            except ValueError:
                                continue
                        
                        if dt_atualizacao:
                            diferenca = datetime.now() - dt_atualizacao
                            if diferenca.days <= 7:
                                logger.info(
                                    f"Linha {idx}: Registro '{registro_original}' atualizado há {diferenca.days} dias "
                                    f"(há 7 dias ou menos). Pulando consulta..."
                                )
                                # Salva em cache informando que NÃO foi consultado nesta rodada (False)
                                val_existente = row.get(headers[col_validade_idx - 1], "")
                                sit_existente = row.get(headers[col_situacao_idx - 1], "") if col_situacao_idx else None
                                classe_existente = row.get(headers[col_classe_idx - 1], "") if col_classe_idx else None
                                cache_consultas[registro_limpo] = (val_existente, sit_existente, classe_existente, False)
                                continue
                    except Exception as e:
                        logger.warning(
                            f"Linha {idx}: Falha ao calcular intervalo da última atualização '{ult_atualizacao_str}': {e}. "
                            f"Processando consulta por segurança..."
                        )

            logger.info(f"Processando linha {idx}: Registro '{registro_original}'...")
            
            # Faz a busca visual no portal
            validade, situacao, classe = search_registry_visual(page, registro_limpo)
            
            # Salva no cache local indicando que FOI consultado nesta rodada (True)
            cache_consultas[registro_limpo] = (validade, situacao, classe, True)
            
            # Acumula os valores correspondentes de volta na memória para atualização em lote
            if validade:
                cells_to_update.append(gspread.cell.Cell(row=idx, col=col_validade_idx, value=validade))
                if col_situacao_idx:
                    cells_to_update.append(gspread.cell.Cell(row=idx, col=col_situacao_idx, value=situacao))
                if col_classe_idx:
                    cells_to_update.append(gspread.cell.Cell(row=idx, col=col_classe_idx, value=classe))
            else:
                cells_to_update.append(gspread.cell.Cell(row=idx, col=col_validade_idx, value="Não Encontrado"))
                if col_situacao_idx:
                    cells_to_update.append(gspread.cell.Cell(row=idx, col=col_situacao_idx, value="Inativo ou Não Encontrado"))
                if col_classe_idx:
                    cells_to_update.append(gspread.cell.Cell(row=idx, col=col_classe_idx, value="Não Encontrada"))
            
            if col_atualizacao_idx:
                data_hoje = datetime.now().strftime("%d/%m/%Y %H:%M")
                cells_to_update.append(gspread.cell.Cell(row=idx, col=col_atualizacao_idx, value=data_hoje))
            
            # Executa a gravação em lote se atingir a capacidade definida
            if len(cells_to_update) >= (batch_size * 2):
                commit_batch(sheet, cells_to_update)
                
            # Rate limiting amigável otimizado
            time.sleep(0.8)
            
        # Grava qualquer célula restante pendente no final da execução
        if cells_to_update:
            commit_batch(sheet, cells_to_update)
            
        browser.close()
        logger.info("Automação concluída com sucesso!")

if __name__ == "__main__":
    main()
