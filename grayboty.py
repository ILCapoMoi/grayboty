"""
GrayPointsBot – Discord bot for tracking Training Points (TP) and Mission Points (MP)
===================================================================
Slash commands (all in English)
------------------------------
* `/showprofile [member]` – shows TP & MP. If *member* omitted, shows yourself.
* `/addtp` – add training points with automatic weighting:
    * **mvp**   → each mention +3 TP
    * **promo** → each mention +2 TP
    * **attended** → each mention +1 TP
    * **rollcall** → link for bookkeeping (stored in the confirmation msg only)
* `/addmp` – add mission points:
    * **member** → mention (one user)
    * **missionpoints** → integer ≥ 1
    * **rollcall** → link for bookkeeping
* `/setup` *(admins only)* – manage which roles can use `/addtp` & `/addmp`:
    * `/setup addrole <role>`
    * `/setup removerole <role>`
    * `/setup list`

All confirmation messages auto‑delete after 10 s to keep channels tidy.

File structure & persistence
---------------------------
```
GrayBot/
├─ bot_points.py    ← this script
├─ points.json      ← {"guild_id": {"user_id": {"tp": int, "mp": int}}}
├─ config.json      ← {"guild_id": [role_id, ...]}
└─ .env             ← DISCORD_TOKEN=xxxxx
```

Requirements
------------
* Python ≥ 3.10
* `pip install -U "discord.py[voice]>=2.4.0"`

-----------------------------------------------------
"""
# ─────────────── Imports ───────────────
import os
import re
import time
import threading
import contextlib
import asyncio
from typing import List, cast

from datetime import datetime, timezone
import aiohttp
import psutil

import discord
from discord import app_commands
from discord.ext import commands

from flask import Flask

from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
from pymongo import ReturnDocument
# ───────────── MongoDB setup ─────────────
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("Environment variable MONGO_URI not set.")

client = MongoClient(MONGO_URI, server_api=ServerApi("1"))
db = client.grayboty_db
points_collection = db.points
config_collection = db.config

def print_db_sizes() -> None:
    dbs = client.list_databases()
    print("\n======= DATABASE SIZES =======")
    for info in dbs:
        mb = round(info["sizeOnDisk"] / (1024 * 1024), 2)
        print(f"{info['name']}: {mb} MB")
    print("==============================\n")

print_db_sizes() # Mostrar el uso de espacio siempre al iniciar

try:
    client.admin.command("ping")
    print("Pinged your deployment. Connected to MongoDB!")
except Exception as e:
    print("Error connecting to MongoDB:", e)

# ───────────── Utilidades MongoDB ─────────────
def get_user_data(gid: int, uid: int):
    doc = points_collection.find_one({"guild_id": gid, "user_id": uid})
    if not doc:
        doc = {"guild_id": gid, "user_id": uid, "tp": 0, "mp": 0}
        points_collection.insert_one(doc)
    else:
        # Asegurarse de que existan las claves 'tp' y 'mp'
        if "tp" not in doc:
            doc["tp"] = 0
        if "mp" not in doc:
            doc["mp"] = 0
    return doc

def add_points(gid: int, uid: int, cat: str, amt: int) -> int:
    """Incrementa TP o MP y devuelve el nuevo total."""
    doc = points_collection.find_one_and_update(
        {"guild_id": gid, "user_id": uid},
        {"$inc": {cat: amt}},
        upsert=True,
        return_document=ReturnDocument.AFTER,  # devuelve el doc actualizado
    )
    return doc[cat]   # total final tras el incremento

def allowed_roles(gid: int) -> List[int]:
    doc = config_collection.find_one({"guild_id": gid}) or {}
    return doc.get("role_ids", [])

def save_allowed_roles(gid: int, role_ids: List[int]) -> None:
    config_collection.update_one(
        {"guild_id": gid},
        {"$set": {"role_ids": role_ids}},
        upsert=True,
    )

# ───────────── Constantes ─────────────
MENTION_RE = re.compile(r"<@!?(\d+)>")
POINT_VALUES = {"mvp": 3, "promo": 2, "attended": 1}

#SABERFORCE_BADGE_ID = 480453722785205
#OG_ROLE_NAME = "OG"
#OG_FECHA_INICIO = datetime(2024, 3, 25, tzinfo=timezone.utc)
#OG_FECHA_FIN = datetime(2024, 11, 10, tzinfo=timezone.utc)


# ───────────── Bot setup ─────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def has_permission(member: discord.Member) -> bool:
    return any(r.id in allowed_roles(member.guild.id) for r in member.roles)
   
# ───────────── Función para obtener el rango más alto ─────────────
rank_list = [
    "Initiate",
    "Acolyte",
    "Disciple",
    "Seeker",
    "Knight",
    "Gray Knight",
    "Silver Knight",
    "Master - On trial",
    "Grandmaster",
    "Master of Balance",
    "Gray Lord",
    "Ashen Lord",
    "Gray Emperor",
    "Elder Gray Emperor"
]
rank_emojis = {
    "Initiate": "<:Initate:1384843420316729435>",
    "Acolyte": "<:Acolyte:1384849435225358407>",
    "Disciple": "<:Disciple:1384843393234108426>",
    "Seeker": "<:Seeker:1384843362942844988>",
    "Knight": "<:Knight:1384844814104789032>",
    "Gray Knight": "<:GrayKnight:1384844842361815111>",
    "Silver Knight": "<:SilverKnight:1384874305363513425>",
    "Master - On trial": "<:trial_master:1390000479970263100>",
    "Grandmaster": "<:grm:1384494486222147654>",
    "Master of Balance": "<:Mbalance:1384835972813820057>",
    "Gray Lord": "<:GrayLord:1395372415856410686>",
    "Ashen Lord": "<:AshenLord:1395372378728431626>",
    "Gray Emperor": "<:Silver:1384690687189975090>",
    "Elder Gray Emperor": "<:gold:1384690646803284038>",
}
def get_highest_rank(member: discord.Member) -> str:
    member_roles = [role.name for role in member.roles]
    ranks_found = [rank for rank in rank_list if rank in member_roles]
    if not ranks_found:
        return "No Rank"
    
    highest_rank = max(ranks_found, key=lambda r: rank_list.index(r))
    emoji = rank_emojis.get(highest_rank, "")
    return f"{emoji} | {highest_rank}" if emoji else highest_rank

# ───────────── /showprofile ─────────────
@bot.tree.command(name="showprofile", description="Show Training & Mission Points")
@app_commands.describe(member="Member to view; leave empty for yourself")
async def showprofile(interaction: discord.Interaction, member: discord.Member | None = None):
    try:
        await interaction.response.defer(thinking=True)
    except discord.errors.InteractionResponded:
        pass

    if member is None:
        member = interaction.user

    data = get_user_data(interaction.guild.id, member.id)

    # Obtener el rango más alto del usuario
    highest_rank = get_highest_rank(member)

    embed = discord.Embed(
        title=f"{member.display_name}",
        color=discord.Color.from_rgb(252, 246, 193)
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    
    embed.add_field(name="Training Points", value=data["tp"], inline=False)
    embed.add_field(name="Mission Points", value=data["mp"], inline=False)
    embed.add_field(name="\u200b", value="────────────", inline=False)
    embed.add_field(name="Rank", value=highest_rank, inline=False)

    msg = await interaction.followup.send(embed=embed)

    await asyncio.sleep(20)
    with contextlib.suppress(discord.Forbidden):
        await msg.delete()

# ───────────── /addtp ─────────────
@bot.tree.command(name="addtp", description="Add Training Points with automatic weighting")
@app_commands.describe(
    mvp="Mentions for MVP (+3 each)",
    promo="Mentions for Promo (+2 each)",
    attended="Mentions for Attendance (+1 each)",
    rollcall="Roll‑call message link",
)
async def addtp(
    interaction: discord.Interaction,
    promo: str,
    rollcall: str,
    mvp: str = "",
    attended: str = "",
):
    caller = cast(discord.Member, interaction.user)

    if not has_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return

    await interaction.response.defer()
    guild = interaction.guild

    summary = []
    for cat, text in {"mvp": mvp, "promo": promo, "attended": attended}.items():
        for uid in MENTION_RE.findall(text):
            member = guild.get_member(int(uid))
            if member:
                total = add_points(guild.id, member.id, "tp", POINT_VALUES[cat])
                summary.append(f"{member.mention} +{POINT_VALUES[cat]} TP → **{total}**")

    if not summary:
        await interaction.followup.send("ℹ️ No valid mentions found.")
        return

    embed = discord.Embed(
        title="✅ Training Points Added",
        description="\n".join(summary) + (f"\n🔗 {rollcall}" if rollcall else ""),
        color=discord.Color.green()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(15)
    with contextlib.suppress(discord.Forbidden):
        await msg.delete()

# ───────────── /addmp ─────────────
@bot.tree.command(name="addmp", description="Add Mission Points to a member")
@app_commands.describe(
    member="Member to receive points",
    missionpoints="Number of points",
    rollcall="Roll‑call link",
)
async def addmp(
    interaction: discord.Interaction,
    member: discord.Member,
    missionpoints: app_commands.Range[int, 1],
    rollcall: str,
):
    caller = cast(discord.Member, interaction.user)
    if not has_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return
    await interaction.response.defer()
    total = add_points(interaction.guild.id, member.id, "mp", missionpoints)
   
    embed = discord.Embed(
        title="✅ Mission Points Added",
        description=f"{member.mention} +{missionpoints} MP → **{total}**" + (f"\n🔗 {rollcall}" if rollcall else ""),
        color=discord.Color.blue()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(15)
    with contextlib.suppress(discord.Forbidden):
        await msg.delete()
       
# ───────────── /deltp ─────────────
@bot.tree.command(name="deltp", description="Remove Training Points from one or more members (Admin only)")
@app_commands.describe(
    members="Mentions or IDs of members separated by spaces",
    points="Points to remove (positive integer)"
)
@app_commands.checks.has_permissions(administrator=True)
async def deltp(interaction: discord.Interaction, members: str, points: app_commands.Range[int, 1]):
    await interaction.response.defer()
    guild = interaction.guild

    member_ids = MENTION_RE.findall(members)
    if not member_ids:
        await interaction.followup.send("❌ No valid member mentions found.", ephemeral=True)
        return

    summary = []
    for mid in member_ids:
        member = guild.get_member(int(mid))
        if not member:
            summary.append(f"User ID {mid} not found in guild.")
            continue
        doc = get_user_data(guild.id, member.id)
        current_tp = doc.get("tp", 0)
        remove_amt = min(points, current_tp)
        if remove_amt > 0:
            new_tp = add_points(guild.id, member.id, "tp", -remove_amt)
            summary.append(f"{member.mention} -{remove_amt} TP → **{new_tp}**")
        else:
            summary.append(f"{member.mention} has no TP to remove.")

    embed = discord.Embed(
        title="⚠️ Training Points Removed",
        description="\n".join(summary),
        color=discord.Color.orange()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(15)
    with contextlib.suppress(discord.Forbidden):
        await msg.delete()
       
# ───────────── /delmp ─────────────
@bot.tree.command(name="delmp", description="Remove Mission Points from one or more members (Admin only)")
@app_commands.describe(
    members="Mentions or IDs of members separated by spaces",
    points="Points to remove (positive integer)"
)
@app_commands.checks.has_permissions(administrator=True)
async def delmp(interaction: discord.Interaction, members: str, points: app_commands.Range[int, 1]):
    await interaction.response.defer()
    guild = interaction.guild

    member_ids = MENTION_RE.findall(members)
    if not member_ids:
        await interaction.followup.send("❌ No valid member mentions found.", ephemeral=True)
        return

    summary = []
    for mid in member_ids:
        member = guild.get_member(int(mid))
        if not member:
            summary.append(f"User ID {mid} not found in guild.")
            continue
        doc = get_user_data(guild.id, member.id)
        current_mp = doc.get("mp", 0)
        remove_amt = min(points, current_mp)
        if remove_amt > 0:
            new_mp = add_points(guild.id, member.id, "mp", -remove_amt)
            summary.append(f"{member.mention} -{remove_amt} MP → **{new_mp}**")
        else:
            summary.append(f"{member.mention} has no MP to remove.")

    embed = discord.Embed(
        title="⚠️ Mission Points Removed",
        description="\n".join(summary),
        color=discord.Color.orange()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(15)
    with contextlib.suppress(discord.Forbidden):
        await msg.delete()

# ───────────── /addall ─────────────
@bot.tree.command(name="addall", description="Add TP and/or MP to one member (Admin only)")
@app_commands.describe(
    member="Member to add points to",
    tp="Training Points to add (optional, default 0)",
    mp="Mission Points to add (optional, default 0)"
)
@app_commands.checks.has_permissions(administrator=True)
async def addall(
    interaction: discord.Interaction,
    member: discord.Member,
    tp: app_commands.Range[int, 0] = 0,
    mp: app_commands.Range[int, 0] = 0,
):
    if tp == 0 and mp == 0:
        await interaction.response.send_message("❌ You must specify at least TP or MP points to add.", ephemeral=True)
        return

    await interaction.response.defer()

    guild_id = interaction.guild.id
    results = []

    if tp > 0:
        new_tp = add_points(guild_id, member.id, "tp", tp)
        results.append(f"✅ {member.mention} +{tp} TP → **{new_tp}**")

    if mp > 0:
        new_mp = add_points(guild_id, member.id, "mp", mp)
        results.append(f"✅ {member.mention} +{mp} MP → **{new_mp}**")

    embed = discord.Embed(
        title="✅ Points Added",
        description="\n".join(results),
        color=discord.Color.yellow()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(15)
    with contextlib.suppress(discord.Forbidden):
        await msg.delete()

# ───────────── Autorización fija (roles permitidos para añadir puntos) ─────────────

# Lista de IDs de roles que pueden usar comandos para añadir puntos.
ALLOWED_ROLE_IDS = [
    1380998711555002469,  # Elder Gray Emperor
    1380998901263499314,  # Gray Emperor
    1385195798576496741,  # Ashen Lord
    1381369825015632023,  # Gray Lord
    1381035805065347093,  # Master of Balance
    1381369333279883384,  # Grandmaster
    1387185214144647409,  # Master - On Trial
]

# Función para verificar si el miembro tiene un rol autorizado
def has_permission(member: discord.Member) -> bool:
    return any(role.id in ALLOWED_ROLE_IDS for role in member.roles)

# ───────────── Eventos ─────────────
@bot.event
async def on_ready():
    print(f"🤖 Bot started as {bot.user} (ID: {bot.user.id}) — connected successfully.")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"❌ Slash command sync error: {e}")

    threading.Thread(target=monitor_bot, daemon=True).start()

# ───────────── Keep‑alive server ─────────────
app = Flask(__name__)

@app.route("/", methods=["GET", "HEAD"])
def home():
    return "Bot is running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))  # Toma el puerto asignado o 8080 por defecto
    app.run(host="0.0.0.0", port=port, debug=False)

threading.Thread(target=run_flask, daemon=True).start()

# ─────── Monitor Bot (check RAM & conexión) ────────
def monitor_bot():
    print("⏳ Waiting 10 minutes before starting monitoring…")
    time.sleep(600)  # Wait 10 minutes
    process = psutil.Process(os.getpid())
    print("🛡️ RAM and connection monitor started.")
    while True:
        try:
            mem_mb = process.memory_info().rss / (1024 * 1024)
            print(f"📦 Memory usage: {mem_mb:.2f} MB")
            if mem_mb >= 490:
                print(f"⚠️ High memory usage detected: {mem_mb:.2f} MB. Restarting…")
                os._exit(1)
            if bot.is_closed() or not bot.is_ready():
                print("❌ Bot not ready. Restarting…")
                os._exit(1)
            print("✅ Bot check passed.", flush=True)
            time.sleep(600)  # Wait between checks (10 min)
        except Exception as e:
            print(f"❌ Error in monitor_bot: {e}")
            time.sleep(10)  # Wait a bit before continuing

# ───────────── Run bot ─────────────
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Environment variable DISCORD_TOKEN not set.")

try:
    bot.run(TOKEN)
except Exception as e:
    print(f"Fatal error running bot: {e}")
    import traceback
    traceback.print_exc()
    os._exit(1)
