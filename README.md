# Automação de Validade de Registros ANVISA

Esta automação foi projetada para ler semanalmente os registros ANVISA listados em uma planilha online do Google Sheets, consultar suas respectivas validades no portal oficial da ANVISA e atualizar a planilha automaticamente.

A execução é gratuita e roda em segundo plano através do **GitHub Actions**.

---

## Passo 1: Preparar a Planilha no Google Sheets

1. Crie ou abra uma planilha no Google Sheets.
2. Certifique-se de que a planilha possui cabeçalhos na primeira linha. Ela precisa conter no mínimo:
   - **Registro ANVISA**: Uma coluna contendo o número do registro (com ou sem pontos/hifens) ou o número do processo.
   - **Validade**: Uma coluna onde o robô escreverá a data de expiração (Ex: `25/12/2028`).
3. *(Opcional)* Você pode adicionar mais duas colunas que serão atualizadas automaticamente:
   - **Situação**: Onde o robô gravará o status do registro (Ex: `ATIVO`, `CANCELADO`).
   - **Última Atualização**: Onde o robô gravará a data e hora da última consulta feita pelo script.
4. **Anote o nome exato da planilha** (Ex: `Minha Planilha de Produtos`) e o **nome exato da aba** (Ex: `Sheet1` ou `Página1`).

---

## Passo 2: Criar a Conta de Serviço no Google Cloud (Grátis)

Para que o robô possa editar a planilha online, precisamos dar permissão a ele através do Google Cloud:

1. Acesse o [Google Cloud Console](https://console.cloud.google.com/).
2. Caso não tenha um projeto, crie um clicando em **Selecionar um projeto** > **Novo Projeto**. Dê um nome a ele (Ex: `Automacao-ANVISA`).
3. No menu lateral esquerdo, vá em **APIs e Serviços** > **Biblioteca**.
4. Pesquise por **Google Sheets API** e clique em **Ativar**.
5. Volte à Biblioteca, pesquise por **Google Drive API** e clique em **Ativar**.
6. Vá em **APIs e Serviços** > **Credenciais**.
7. Clique em **+ Criar Credenciais** no topo e selecione **Conta de Serviço**.
8. Preencha o nome da conta (Ex: `sheets-bot`) e clique em **Criar e Continuar** e depois em **Concluir**.
9. Na tabela "Contas de serviço", clique no e-mail criado.
10. Vá na aba **Chaves** (Keys) > **Adicionar Chave** > **Criar nova chave**.
11. Selecione o formato **JSON** e clique em **Criar**.
12. O download de um arquivo contendo as credenciais (Ex: `automacao-anvisa-xxxxxx.json`) será feito. **Guarde este arquivo**, ele possui a chave secreta.

---

## Passo 3: Compartilhar a Planilha

1. Abra o arquivo JSON de credenciais baixado e localize o campo `"client_email"` (ele se parece com: `sheets-bot@seu-projeto.iam.gserviceaccount.com`).
2. Abra a sua planilha do Google Sheets e clique no botão **Compartilhar** no canto superior direito.
3. Cole o e-mail da Conta de Serviço no campo de convite.
4. Defina a permissão como **Editor** e desmarque "Notificar pessoas" para evitar erros.
5. Clique em **Compartilhar**.

---

## Passo 4: Hospedar o Código e Configurar o GitHub

Para fazer a automação rodar de graça toda semana na nuvem:

1. Crie um repositório no GitHub (pode ser **Privado**).
2. Suba os arquivos do projeto para o repositório (`main.py`, `requirements.txt`, `.github/workflows/weekly_check.yml`). **Atenção:** NÃO suba o arquivo `credentials.json` para o GitHub por motivos de segurança.
3. No seu repositório do GitHub, clique em **Settings** (Configurações) no topo.
4. No menu lateral esquerdo, vá em **Secrets and variables** > **Actions**.
5. Clique em **New repository secret** para adicionar cada um destes 3 segredos:

| Nome do Segredo | Descrição |
| :--- | :--- |
| `SPREADSHEET_NAME` | O nome exato do arquivo da sua planilha no Google Sheets (Ex: `Minha Planilha de Produtos`). |
| `SHEET_TAB_NAME` | O nome da aba específica da planilha (Ex: `Sheet1` ou `Página1`). Se não preenchido, usará `Sheet1`. |
| `GOOGLE_CREDENTIALS` | Abra o arquivo JSON de credenciais que você baixou, copie **todo** o conteúdo e cole-o aqui dentro deste segredo. |

---

## Como Funciona e Como Executar Manualmente

* **Automático**: O GitHub Actions executará o script de forma silenciosa todas as segundas-feiras às 00:00 UTC (21h de domingo no horário de Brasília).
* **Manual**: Se você quiser rodar o script imediatamente para testar:
  1. Vá na aba **Actions** do seu repositório no GitHub.
  2. Clique em **ANVISA Registry Expiration Check** na barra lateral.
  3. Clique no botão **Run workflow** no canto direito e confirme em **Run workflow**.
  4. A execução iniciará na hora e você poderá acompanhar o log do processo em tempo real.
