import pandas as pd
# alternative à pandas
import polars as pl
from sqlalchemy import create_engine, text
from tqdm import tqdm

# Connexion à la base de données pgsql
engine = create_engine("postgresql+psycopg2://postgres:@localhost:5432/dw_shopnow")

with engine.begin() as conn:
    print("🧹 Suppression des anciens schémas ...")
    conn.execute(text("DROP SCHEMA IF EXISTS staging CASCADE;"))
    conn.execute(text("DROP SCHEMA IF EXISTS dwh CASCADE;"))

    print("🧱 Recréation des schémas ...")
    conn.execute(text("CREATE SCHEMA staging;"))
    conn.execute(text("CREATE SCHEMA dwh;"))

print("✅ Schémas recréés avec succès, prêt pour l'ETL !")

# ----------------------------------------------------------------------------
# Lecture du fichier csv
print('Chargement du fichier data.csv ...')
df = pd.read_csv('../data.csv', encoding='latin1')

print('Chargement du fichier data.csv terminé !')

# Avec polars, infer_schema_length permet de donner un nombre de ligne à parcourir avant de faire
# l'inférence histoire de savoir si c'est la bonne inférence à faire
# df_wpl = pl.read_csv('../data.csv', encoding='latin1', infer_schema_length=10000)
print(df.head())

# Standardisation des colonnes
df.columns = [c.strip().lower() for c in df.columns]

# Chargement brut dans STAGING
print("📦 Chargement dans staging.sales_raw ...")
df.to_sql("sales_raw", engine, schema="staging", if_exists="replace", index=False)
print("✅ Données brutes chargées dans staging.sales_raw")

# Nettoyage et transformations
print("🧹 Nettoyage des données ...")
df = df.dropna(subset=["invoiceno", "stockcode", "description", "quantity", "invoicedate", "unitprice", "customerid"])
df["invoicedate"] = pd.to_datetime(df["invoicedate"], errors="coerce")
df = df[df["quantity"] > 0]
df = df[df["unitprice"] > 0]

#   Création des dimensions
print("🧱 Création des tables de dimensions ...")

dim_product = df[["stockcode", "description"]].drop_duplicates().reset_index(drop=True)
dim_product["product_id"] = dim_product.index + 1

dim_customer = df[["customerid", "country"]].drop_duplicates().reset_index(drop=True)
dim_customer["customer_id"] = dim_customer.index + 1

dim_date = df[["invoicedate"]].drop_duplicates().reset_index(drop=True)
dim_date["date_id"] = dim_date.index + 1
dim_date["year"] = dim_date["invoicedate"].dt.year
dim_date["month"] = dim_date["invoicedate"].dt.month
dim_date["day"] = dim_date["invoicedate"].dt.day

# Sauvegarde des dimensions dans le schéma DWH
print("💾 Sauvegarde des dimensions ...")
dim_product.to_sql("dim_product", engine, schema="dwh", if_exists="replace", index=False)
dim_customer.to_sql("dim_customer", engine, schema="dwh", if_exists="replace", index=False)
dim_date.to_sql("dim_date", engine, schema="dwh", if_exists="replace", index=False)

# Création de la table de faits
print("📊 Création de fact_sales ...")

# Dictionnaires pour map rapide (sans jointure SQL)
prod_map = dict(zip(dim_product["stockcode"], dim_product["product_id"]))
cust_map = dict(zip(dim_customer["customerid"], dim_customer["customer_id"]))
date_map = dict(zip(dim_date["invoicedate"], dim_date["date_id"]))

fact = df.copy()
fact["product_id"] = fact["stockcode"].map(prod_map)
fact["customer_id"] = fact["customerid"].map(cust_map)
fact["date_id"] = fact["invoicedate"].map(date_map)
fact["total_amount"] = fact["quantity"] * fact["unitprice"]

fact_sales = fact[["invoiceno", "date_id", "product_id", "customer_id", "quantity", "unitprice", "total_amount"]]

# Chargement progressif en lots
print("🚀 Chargement de fact_sales dans PostgreSQL ...")
for start in tqdm(range(0, len(fact_sales), 50000)):
    end = start + 50000
    fact_sales.iloc[start:end].to_sql("fact_sales", engine, schema="dwh", if_exists="append", index=False)

print("✅ ETL terminé avec succès !")
