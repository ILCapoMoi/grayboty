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
import asyncio
import time
import threading
import contextlib
from typing import List, cast

from datetime import datetime
import aiohttp

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

if os.getenv("DEBUG_DB_SIZES") == "1":
    print_db_sizes()

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

BADGE_ID = "480453722785205"
OG_ROLE_NAME = "OG"
OG_FECHA_INICIO = datetime(2024, 11, 10)
OG_FECHA_FIN = datetime(2024, 12, 31)

# ───────────── Bot setup ─────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def has_permission(member: discord.Member) -> bool:
    return any(r.id in allowed_roles(member.guild.id) for r in member.roles)

# ───────────── /showprofile ─────────────
@bot.tree.command(name="showprofile", description="Show Training & Mission Points")
@app_commands.describe(member="Member to view; leave empty for yourself")
async def showprofile(interaction: discord.Interaction, member: discord.Member | None = None):
    try:
        await interaction.response.defer(thinking=True)
    except discord.errors.InteractionResponded:
        # Ya respondido, seguimos sin defer
        pass

    if member is None:
        member = interaction.user

    data = get_user_data(interaction.guild.id, member.id)

    embed = discord.Embed(title=f"Profile – {member.display_name}")
    embed.add_field(name="Training Points", value=data["tp"])
    embed.add_field(name="Mission Points", value=data["mp"])

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

    msg = await interaction.followup.send(
        "\n".join(summary) + (f"\n🔗 {rollcall}" if rollcall else "")
    )
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
    msg = await interaction.followup.send(
        f"{member.mention} +{missionpoints} MP → **{total}**"
        + (f"\n🔗 {rollcall}" if rollcall else "")
    )
    await asyncio.sleep(15)
    with contextlib.suppress(discord.Forbidden):
        await msg.delete()

# ───────────── Utility: get Roblox user ID from username ─────────────
async def get_roblox_user_id(username: str) -> str | None:
    url = "https://users.roblox.com/v1/usernames/users"
    payload = {"usernames": [username], "excludeBannedUsers": True}
    headers = {"Content-Type": "application/json"}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                print(f"⚠️ Error fetching user ID for {username}: HTTP {resp.status}")
                return None
            data = await resp.json()

    if data.get("data") and len(data["data"]) > 0:
        return str(data["data"][0]["id"])
    return None

# 2. USER ID → BADGE DATE
async def obtener_fecha_badge(user_id: str) -> datetime | None:
    url = f"https://www.roblox.com/users/{user_id}/badges"
    headers = {"User-Agent": "Mozilla/5.0"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                print(f"⚠️ Error fetching badge page: HTTP {resp.status}")
                return None
            html = await resp.text()

    bloque = re.search(r'(?s)<h3 class="badge-title">.*?SaberForce Tester.*?</li>', html)
    if not bloque:
        print("❌ Badge block not found in HTML.")
        return None

    bloque = bloque.group()
    fecha_match = re.search(r"Awarded:\s*(\w+\s\d{1,2},\s\d{4})", bloque)
    if not fecha_match:
        print("❌ Badge date not found.")
        return None

    try:
        return datetime.strptime(fecha_match.group(1), "%b %d, %Y")
    except ValueError as e:
        print("❌ Error parsing badge date:", e)
        return None

# ───────────── /verifyog ─────────────
@bot.tree.command(name="verifyog", description="Verify if a member earned the OG SaberForce badge")
@app_commands.describe(member="Member to verify")
async def verifyog(interaction: discord.Interaction, member: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only administrators can use this command.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    try:
        print("⚙️ Step 1: Member selected:", member.display_name)

        # Check if the nickname includes (@RobloxName); if not, use the whole name
        username_match = re.search(r"\(@(\w+)\)", member.display_name)
        if username_match:
            roblox_username = username_match.group(1)
        else:
            roblox_username = member.display_name.strip()
            print("ℹ️ No parentheses detected, assuming full display name is Roblox username.")

        print("🔍 Extracted Roblox username:", roblox_username)

        roblox_id = await get_roblox_user_id(roblox_username)
        if not roblox_id:
            await interaction.followup.send(f"❌ Could not find a Roblox account named `{roblox_username}`.")
            return
        print("🔍 Roblox user ID:", roblox_id)

        date = await obtener_fecha_badge(roblox_id)
        if not date:
            await interaction.followup.send(
                f"⚠️ The badge was not found on the Roblox profile of `{roblox_username}`."
            )
            return
        print("📅 Badge date:", date.strftime("%Y-%m-%d"))

        if OG_FECHA_INICIO <= date <= OG_FECHA_FIN:
            og_role = discord.utils.get(interaction.guild.roles, name=OG_ROLE_NAME)
            if not og_role:
                await interaction.followup.send("❌ The `OG` role was not found in this server.")
                return

            if og_role not in member.roles:
                await member.add_roles(og_role, reason="Verified as OG by badge")
                await interaction.followup.send(
                    f"✅ {member.mention} earned the badge on **{date.strftime('%d-%m-%Y')}**. OG role granted."
                )
            else:
                await interaction.followup.send(
                    f"✅ {member.mention} already has the OG role. Badge date: **{date.strftime('%d-%m-%Y')}**."
                )
        else:
            await interaction.followup.send(
                f"⚠️ {member.mention} has the badge, but the date (**{date.strftime('%d-%m-%Y')}**) is outside the OG badge period."
            )

    except Exception as e:
        print("🔥 Error in /verifyog:", e)
        await interaction.followup.send(f"❌ Internal error: `{e}`. Check the logs.")

# ───────────── Setup group (/setup …) ─────────────
class Setup(app_commands.Group, name="setup", description="Configure roles allowed to add points"):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = cast(discord.Member, interaction.user)
        if not member.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="addrole", description="Authorize a role")
    async def addrole(self, interaction: discord.Interaction, role: discord.Role):
        roles = allowed_roles(interaction.guild.id)
        if role.id not in roles:
            roles.append(role.id)
            save_allowed_roles(interaction.guild.id, roles)
            msg = f"✅ {role.mention} authorized."
        else:
            msg = f"ℹ️ {role.mention} already authorized."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="removerole", description="Remove a role from authorization list")
    async def removerole(self, interaction: discord.Interaction, role: discord.Role):
        roles = allowed_roles(interaction.guild.id)
        if role.id in roles:
            roles.remove(role.id)
            save_allowed_roles(interaction.guild.id, roles)
            msg = f"✅ {role.mention} removed."
        else:
            msg = f"ℹ️ {role.mention} was not in the list."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="list", description="Show authorized roles")
    async def listroles(self, interaction: discord.Interaction):
        ids = allowed_roles(interaction.guild.id)
        if not ids:
            await interaction.response.send_message("🔸 No authorized roles.", ephemeral=True)
            return
        mentions = [interaction.guild.get_role(rid).mention for rid in ids if interaction.guild.get_role(rid)]
        await interaction.response.send_message("🔸 Authorized roles:\n" + "\n".join(mentions), ephemeral=True)

bot.tree.add_command(Setup())

# ───────────── Eventos ─────────────
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print("Slash‑command sync error:", e)

# ───────────── Keep‑alive server ─────────────
app = Flask(__name__)

@app.route("/", methods=["GET", "HEAD"])
def home():
    return "Bot is running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))  # Toma el puerto asignado o 8080 por defecto
    app.run(host="0.0.0.0", port=port, debug=False)

threading.Thread(target=run_flask, daemon=True).start()

# ───────────── Auto‑restart checker ─────────────
def auto_restart_check():
    while True:
        time.sleep(300)  # 5 min
        if bot.is_closed() or not bot.is_ready():
            print("❌ Bot no está listo. Reiniciando…")
            os._exit(1)
        print("✅ Bot verificado correctamente.", flush=True)

threading.Thread(target=auto_restart_check, daemon=True).start()

# ───────────── Run bot ─────────────
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Environment variable DISCORD_TOKEN not set.")
bot.run(TOKEN)
