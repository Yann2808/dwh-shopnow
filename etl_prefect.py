from prefect import task, flow
import pandas as pd
from sqlalchemy import create_engine, text
import os
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv()

def get_engine():
    """Crée une engine SQLAlchemy en utilisant les variables d'environnement."""
    user = os.getenv("PG_USER")
    password = quote_plus(os.getenv("PG_PASSWORD"))
    host = os.getenv("PG_HOST")
    port = os.getenv("PG_PORT")
    database = os.getenv("PG_DATABASE")

    connection_string = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"
    return create_engine(connection_string)


# 1️ Tâche de lecture du fichier data.csv
@task
def read_data(filepath):
    """Lit un fichier CSV et renvoie un DataFrame pandas."""
    print("📥 Lecture du fichier...")
    df = pd.read_csv(filepath, encoding="latin1")
    print(f"✅ {len(df)} lignes lues.")
    return df

#   Tâche de nettoyage de data.csv
@task
def clean_data(df):
    """Nettoie les données brutes avant le chargement."""
    print("🧹 Nettoyage des données...")

    # Supprimer les lignes avec des valeurs manquantes sur les colonnes clés
    df = df.dropna(subset=["InvoiceNo", "StockCode", "Description", "Quantity", "InvoiceDate", "UnitPrice", "CustomerID"])

    # Corriger les types
    df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"], errors="coerce")

    # Supprimer les valeurs aberrantes
    df = df[df["Quantity"] > 0]
    df = df[df["UnitPrice"] > 0]

    print(f"✅ {len(df)} lignes restantes après nettoyage.")

    #   Standardisation des noms de colonnes
    df.columns = df.columns.str.lower()
    return df


# Tâche d'envoi du contenu néttoyé dans le schéma staging
@task
def load_to_staging(df):
    """Charge les données nettoyées dans le schéma staging de PostgreSQL."""
    print("📦 Chargement des données dans staging...")

    # Connexion à la base
    engine = get_engine()
    # Écriture dans staging.retail_cleaned
    df.to_sql("retail_cleaned", engine, schema="staging", if_exists="replace", index=False)

    print("✅ Données chargées dans staging.retail_cleaned.")


#   Tâche pour le création des tables de dimension dans ma BDD
@task
def build_dwh():
    """Construit les tables du Data Warehouse à partir du staging."""
    print("🏗️ Construction du Data Warehouse...")

    engine = get_engine()

    with engine.connect() as conn:
        # 1️⃣ Dimension Produit
        conn.execute(
            text("""
                DROP TABLE IF EXISTS dwh.dim_product CASCADE;
                CREATE TABLE dwh.dim_product AS
                SELECT DISTINCT
                    ROW_NUMBER() OVER() AS product_id,
                    stockcode,
                    description
                FROM staging.retail_cleaned;
        """)
        )

        # 2️⃣ Dimension Client
        conn.execute(
            text("""
                DROP TABLE IF EXISTS dwh.dim_customer CASCADE;
                CREATE TABLE dwh.dim_customer AS
                SELECT DISTINCT
                    ROW_NUMBER() OVER() AS customer_id,
                    customerid AS customer_code,
                    country
                FROM staging.retail_cleaned;
            """)
        )

        # 3️⃣ Dimension Date
        conn.execute(
            text("""
                DROP TABLE IF EXISTS dwh.dim_date CASCADE;
                CREATE TABLE dwh.dim_date AS
                SELECT DISTINCT
                    ROW_NUMBER() OVER() AS date_id,
                    invoicedate::date AS date,
                    EXTRACT(year FROM invoicedate) AS year,
                    EXTRACT(month FROM invoicedate) AS month,
                    EXTRACT(day FROM invoicedate) AS day
                FROM staging.retail_cleaned;
            """)
        )

        # 4️⃣ Fait des ventes
        conn.execute(
            text("""
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
            """)
        )

    print("✅ DWH construit avec succès.")




# 2️ Définition du pipeline (flow)
@flow
def etl_flow():
    """Orchestration de toutes les tâches du pipeline."""
    data = read_data("data/data.csv")

    # appeler la task clean_data
    df_clean = clean_data(data)

    #   appel de load_to_staging pour charger les données nettoyer dans le schéma staging
    load_to_staging(df_clean)

    build_dwh()

    # print(df_clean.head())  # juste pour vérifier que ça marche

# 3️ Lancer le pipeline
if __name__ == "__main__":
    etl_flow()
