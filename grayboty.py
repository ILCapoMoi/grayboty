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
def get_user_data(gid: int, uid: int) -> dict | None:
    doc = points_collection.find_one({"guild_id": gid, "user_id": uid})
    return doc

def add_points(gid, uid, field, amount):
    doc = points_collection.find_one_and_update(
        {"guild_id": gid, "user_id": uid},
        {"$inc": {field: amount}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc[field]

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

# ───────────── Bot setup ─────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def has_permission(member: discord.Member) -> bool:
    return any(r.id in allowed_roles(member.guild.id) for r in member.roles)
   
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

# ────────────── Requisitos para cada rango ──────────────
rank_requirements = {
    "Acolyte": {"tp": 1},
    "Disciple": {"tp": 5, "mp": 3, "tier": "Low-Tier"},
    "Seeker": {"tp": 12, "mp": 5, "tier": "Low-Tier"},
    "Knight": {"tp": 18, "mp": 9, "tier": "Middle-Tier"},
    "Gray Knight": {"tp": 24, "mp": 12, "tier": "Middle-Tier [ ⁑ ]"},
    "Silver Knight": {"tp": 30, "mp": 18, "tier": "Middle-Tier [ ⁂ ]"}
}

# ─────────── /showprofile ───────────
@bot.tree.command(name="showprofile", description="Show Training & Mission Points")
@app_commands.describe(member="Member to view; leave empty for yourself")
async def showprofile(interaction: discord.Interaction, member: discord.Member | None = None):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True)
    except (discord.errors.InteractionResponded, discord.NotFound):
        pass

    if member is None:
        member = interaction.user

    # Buscar el perfil SIN crear (no upsert)
    doc = points_collection.find_one({"guild_id": interaction.guild.id, "user_id": member.id})
    
    if not doc or (doc.get("tp", 0) == 0 and doc.get("mp", 0) == 0):
        # Usuario no tiene perfil ni puntos, mostrar mensaje y salir
        msg = await interaction.followup.send(
            f"_**{member.display_name}** does not have any points to show their profile yet._"
        )
        await asyncio.sleep(15)
        with contextlib.suppress((discord.Forbidden, discord.NotFound)):
            await msg.delete()
        return

    # Orden de medallas (roles y emojis)
    medal_roles_emojis = [
        ("TGO Medal Of Honor", "<:tgohonor:1394844480258965664>"),
        ("Moi Medal Of Honor", "<:moihonor:1394844519589216357>"),
        ("Clxp Medal Of Honor", "<:clxphonor:1394844536022237244>"),
        ("Michi's Medal Of Honor", "<:michishonor:1394844563499122728>"),
        ("Ashen Medal Of Honor", "<:ashenhonor:1394844598928670810>"),
        ("Gray Medal Of Honor", "<:grayhonor:1394844626661277736>"),
    ]

    member_roles = {role.name for role in member.roles}
    medals_present = [emoji for role_name, emoji in medal_roles_emojis if role_name in member_roles]

    medals_text = ""
    if medals_present:
        medals_text = "**Medals of honor**\n| " + " | ".join(medals_present) + " |"

    # Perfil existe con puntos, mostrar info
    highest_rank_raw = get_highest_rank(member)
    current_rank = highest_rank_raw.split("|")[-1].strip() if "|" in highest_rank_raw else highest_rank_raw

    embed = discord.Embed(
        title=f"{member.display_name}",
        color=discord.Color.from_rgb(247, 240, 172)
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(
        name="",
        value=(
            f"Training Points: {doc.get('tp', 0)}\n"
            f"Mission Points: {doc.get('mp', 0)}\n"
            f"<:H1Laser:1395749428135985333><:H2Laser:1395749449753563209>"
            f"<:R1Laser:1395746456681578628><:R1Laser:1395746456681578628>"
            f"<:R1Laser:1395746456681578628><:R1Laser:1395746456681578628>"
            f"<:R2Laser:1395746474293198949>\n"
            f"{medals_text}"
        ),
        inline=False
    )

    next_rank = None
    if current_rank in rank_list:
        current_index = rank_list.index(current_rank)
        if current_index + 1 < len(rank_list):
            next_rank = rank_list[current_index + 1]

    requirement_text = ""
    if next_rank in rank_requirements:
        req = rank_requirements[next_rank]
        requirement_text = (
            f"**Rank:**\n"
            f"{rank_emojis.get(current_rank, '')} | {current_rank}\n\n"
            f"**Next rank requirements:** {rank_emojis.get(next_rank, '')} | {next_rank}\n"
            f"· _**{req.get('tp', 0)}** training points_\n"
            f"· _**{req.get('mp', 0)}** mission points_\n"
        )
        if req.get('tier'):
            requirement_text += f"· _**{req['tier']}** level_"
    elif current_rank == "Silver Knight" and next_rank == "Master - On trial":
        requirement_text = (
            "From this point on, promotions are based on selection by High Ranks (HR). "
            "If you achieve the Level-Tier: **High-Tier**, you may join the elite division: __The Secret Fier__."
        )

    rank_section = f"**_Rank:_**\n{rank_emojis.get(current_rank, '')} | {current_rank}"   # Mostrar siempre el rango actual
    if requirement_text:
        embed.add_field(name="", value=f"{rank_section}\n\n{requirement_text}", inline=False)
    else:
        embed.add_field(name="", value=rank_section, inline=False)

    msg = await interaction.followup.send(embed=embed)

    await asyncio.sleep(25)
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
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
        pts_to_add = POINT_VALUES.get(cat, 0)
        if pts_to_add <= 0:
            continue

        for uid in MENTION_RE.findall(text):
            member = guild.get_member(int(uid))
            if member:
                total = add_points(guild.id, member.id, "tp", pts_to_add)
                summary.append(f"{member.mention} +{pts_to_add} TP → **{total}**")

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
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
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

    # Solo añadir puntos si missionpoints es mayor que 0 (por seguridad)
    if missionpoints > 0:
        total = add_points(interaction.guild.id, member.id, "mp", missionpoints)
    else:
        total = 0

    embed = discord.Embed(
        title="✅ Mission Points Added",
        description=f"{member.mention} +{missionpoints} MP → **{total}**" + (f"\n🔗 {rollcall}" if rollcall else ""),
        color=discord.Color.blue()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(15)
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
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
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
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
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
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
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
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
        print(f"☑️ Synced {len(synced)} slash commands.")
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
    print("⏳ Waiting 5 minutes before starting monitoring…")
    time.sleep(300)  # Wait 5 minutes
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
