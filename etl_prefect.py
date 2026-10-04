from prefect import task, flow, get_run_logger
import pandas as pd
from sqlalchemy import create_engine, text
import os
import requests
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv()

DATA_PATH = os.getenv("DATA_PATH", "data/data.csv")


def get_engine():
    """Crée une engine SQLAlchemy en utilisant les variables d'environnement."""
    user = os.getenv("PG_USER")
    password = quote_plus(os.getenv("PG_PASSWORD"))
    host = os.getenv("PG_HOST")
    port = os.getenv("PG_PORT")
    database = os.getenv("PG_DATABASE")

    connection_string = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"
    return create_engine(connection_string)


# 0️ Création des schémas si besoin (base neuve, ex. conteneur Docker)
@task(retries=3, retry_delay_seconds=10)
def init_schemas():
    """Crée les schémas staging et dwh s'ils n'existent pas."""
    with get_engine().begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS staging;"))
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS dwh;"))


# 1️ Tâche de lecture du fichier data.csv
@task
def read_data(filepath):
    """Lit un fichier CSV et renvoie un DataFrame pandas."""
    logger = get_run_logger()
    logger.info("📥 Lecture du fichier...")
    df = pd.read_csv(filepath, encoding="latin1", dtype={"InvoiceNo": str, "StockCode": str})
    logger.info(f"✅ {len(df)} lignes lues.")
    return df


#   Tâche de nettoyage de data.csv
@task
def clean_data(df):
    """Nettoie les données brutes avant le chargement."""
    logger = get_run_logger()
    logger.info("🧹 Nettoyage des données...")

    # Supprimer les lignes avec des valeurs manquantes sur les colonnes clés
    df = df.dropna(subset=["InvoiceNo", "StockCode", "Description", "Quantity", "InvoiceDate", "UnitPrice", "CustomerID"]).copy()

    # Corriger les types
    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"], errors="coerce")
    df = df.dropna(subset=["InvoiceDate"])
    df["CustomerID"] = df["CustomerID"].astype(int)

    # Supprimer les valeurs aberrantes
    df = df[df["Quantity"] > 0]
    df = df[df["UnitPrice"] > 0]

    logger.info(f"✅ {len(df)} lignes restantes après nettoyage.")

    #   Standardisation des noms de colonnes
    df.columns = df.columns.str.lower()
    return df


# Tâche d'envoi du contenu néttoyé dans le schéma staging
@task(retries=3, retry_delay_seconds=10)
def load_to_staging(df):
    """Charge les données nettoyées dans le schéma staging de PostgreSQL."""
    logger = get_run_logger()
    logger.info("📦 Chargement des données dans staging...")

    # Écriture dans staging.retail_cleaned
    df.to_sql("retail_cleaned", get_engine(), schema="staging", if_exists="replace", index=False, chunksize=10_000)

    logger.info("✅ Données chargées dans staging.retail_cleaned.")
    return len(df)


#   Tâche pour le création des tables de dimension dans ma BDD
@task(retries=3, retry_delay_seconds=10)
def build_dwh():
    """Construit les tables du Data Warehouse à partir du staging."""
    logger = get_run_logger()
    logger.info("🏗️ Construction du Data Warehouse...")

    # engine.begin() : transaction validée (COMMIT) en sortie de bloc,
    # sinon PostgreSQL annule aussi les CREATE TABLE.
    with get_engine().begin() as conn:
        # 1️⃣ Dimension Produit — une ligne par stockcode (dédoublonnage AVANT la numérotation)
        conn.execute(text("""
            DROP TABLE IF EXISTS dwh.dim_product CASCADE;
            CREATE TABLE dwh.dim_product AS
            SELECT
                ROW_NUMBER() OVER (ORDER BY stockcode) AS product_id,
                stockcode,
                description
            FROM (
                SELECT stockcode, MIN(description) AS description
                FROM staging.retail_cleaned
                GROUP BY stockcode
            ) p;
            ALTER TABLE dwh.dim_product ADD PRIMARY KEY (product_id);
        """))

        # 2️⃣ Dimension Client — une ligne par client
        conn.execute(text("""
            DROP TABLE IF EXISTS dwh.dim_customer CASCADE;
            CREATE TABLE dwh.dim_customer AS
            SELECT
                ROW_NUMBER() OVER (ORDER BY customerid) AS customer_id,
                customerid AS customer_code,
                country
            FROM (
                SELECT customerid, MIN(country) AS country
                FROM staging.retail_cleaned
                GROUP BY customerid
            ) c;
            ALTER TABLE dwh.dim_customer ADD PRIMARY KEY (customer_id);
        """))

        # 3️⃣ Dimension Date — une ligne par jour
        conn.execute(text("""
            DROP TABLE IF EXISTS dwh.dim_date CASCADE;
            CREATE TABLE dwh.dim_date AS
            SELECT
                ROW_NUMBER() OVER (ORDER BY date) AS date_id,
                date,
                EXTRACT(year FROM date)::int AS year,
                EXTRACT(month FROM date)::int AS month,
                EXTRACT(day FROM date)::int AS day
            FROM (
                SELECT DISTINCT invoicedate::date AS date
                FROM staging.retail_cleaned
            ) d;
            ALTER TABLE dwh.dim_date ADD PRIMARY KEY (date_id);
        """))

        # 4️⃣ Fait des ventes
        conn.execute(text("""
            DROP TABLE IF EXISTS dwh.fact_sales CASCADE;
            CREATE TABLE dwh.fact_sales AS
            SELECT
                s.invoiceno,
                p.product_id,
                c.customer_id,
                d.date_id,
                s.quantity,
                s.unitprice,
                s.quantity * s.unitprice AS total_amount
            FROM staging.retail_cleaned s
            JOIN dwh.dim_product p ON s.stockcode = p.stockcode
            JOIN dwh.dim_customer c ON s.customerid = c.customer_code
            JOIN dwh.dim_date d ON s.invoicedate::date = d.date;
            ALTER TABLE dwh.fact_sales
                ADD FOREIGN KEY (product_id) REFERENCES dwh.dim_product (product_id),
                ADD FOREIGN KEY (customer_id) REFERENCES dwh.dim_customer (customer_id),
                ADD FOREIGN KEY (date_id) REFERENCES dwh.dim_date (date_id);
        """))

    logger.info("✅ DWH construit avec succès.")


#   Contrôle qualité : la table de faits doit avoir exactement une ligne par ligne de staging
@task
def check_dwh(expected_rows):
    """Vérifie l'intégrité du DWH (pas de lignes perdues ni dupliquées par les jointures)."""
    logger = get_run_logger()
    with get_engine().connect() as conn:
        fact_rows = conn.execute(text("SELECT COUNT(*) FROM dwh.fact_sales")).scalar_one()
        dup_products = conn.execute(text(
            "SELECT COUNT(*) - COUNT(DISTINCT stockcode) FROM dwh.dim_product"
        )).scalar_one()

    if fact_rows != expected_rows or dup_products != 0:
        raise ValueError(
            f"Contrôle qualité KO : {fact_rows} lignes de faits pour {expected_rows} en staging, "
            f"{dup_products} produit(s) en double."
        )
    logger.info(f"✅ Contrôle qualité OK : {fact_rows} lignes de faits.")


#   Alerting : notification webhook (Slack, Discord, Teams...) si ALERT_WEBHOOK_URL est définie
def notify(flow, flow_run, state):
    url = os.getenv("ALERT_WEBHOOK_URL")
    message = f"[ShopNow ETL] {flow_run.name} → {state.name}"
    print(f"🔔 {message}")
    if url:
        try:
            requests.post(url, json={"text": message, "content": message}, timeout=10)
        except requests.RequestException as exc:
            print(f"⚠️ Webhook injoignable : {exc}")


# 2️ Définition du pipeline (flow)
@flow(name="shopnow-etl", on_completion=[notify], on_failure=[notify])
def etl_flow(filepath: str = DATA_PATH):
    """Orchestration de toutes les tâches du pipeline."""
    init_schemas()

    data = read_data(filepath)

    # appeler la task clean_data
    df_clean = clean_data(data)

    #   appel de load_to_staging pour charger les données nettoyer dans le schéma staging
    rows = load_to_staging(df_clean)

    build_dwh()

    check_dwh(rows)


# 3️ Lancer le pipeline
#   - par défaut : une exécution
#   - si SCHEDULE_CRON est défini (ex. "0 6 * * *") : exécution planifiée via Prefect
if __name__ == "__main__":
    cron = os.getenv("SCHEDULE_CRON")
    if cron:
        etl_flow.serve(name="shopnow-etl-scheduled", cron=cron)
    else:
        etl_flow()
