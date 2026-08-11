import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def daily_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    g = df.groupby("Date").agg(Calories=("Calories", "sum"), Protein_g=("Protein_g", "sum")).reset_index()
    return g.sort_values("Date")


def build_chart(df: pd.DataFrame, title: str):
    g = daily_summary(df)
    if g.empty:
        return None

    fig, ax1 = plt.subplots(figsize=(8, 4.5))
    x = g["Date"].astype(str)
    ax1.bar(x, g["Calories"], color="#4C72B0", label="Calories (kcal)")
    ax1.set_ylabel("Calories (kcal)", color="#4C72B0")
    ax1.tick_params(axis="x", rotation=45)

    ax2 = ax1.twinx()
    ax2.plot(x, g["Protein_g"], color="#DD8452", marker="o", linewidth=2, label="Protein (g)")
    ax2.set_ylabel("Protein (g)", color="#DD8452")

    plt.title(title)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def summary_text(df: pd.DataFrame) -> str:
    if df.empty:
        return "No entries in this period yet."

    total_cal = df["Calories"].sum()
    total_pro = df["Protein_g"].sum()
    days = df["Date"].nunique()
    avg_cal = total_cal / days if days else 0
    avg_pro = total_pro / days if days else 0

    lines = [
        f"Days logged: {days}",
        f"Total: {total_cal:.0f} kcal, {total_pro:.0f} g protein",
        f"Daily avg: {avg_cal:.0f} kcal, {avg_pro:.0f} g protein",
        "",
        "Day-wise:",
    ]
    g = daily_summary(df)
    for _, row in g.iterrows():
        lines.append(f"  {row['Date']}: {row['Calories']:.0f} kcal, {row['Protein_g']:.0f} g protein")
    return "\n".join(lines)
