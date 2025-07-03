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
# from dotenv import load_dotenv
# load_dotenv()

import os
import re
import asyncio
import time
import threading
from typing import Dict, List, cast

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
from threading import Thread

# --- MongoDB ---
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi

# ------------ MongoDB Setup ------------

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("Environment variable MONGO_URI not set.")

client = MongoClient(MONGO_URI, server_api=ServerApi('1'))
db = client.grayboty_db
points_collection = db.points
config_collection = db.config

# --- Print database sizes (for debug only) ---
def print_db_sizes():
    dbs = client.list_databases()
    print("\n======= DATABASE SIZES =======")
    for db in dbs:
        name = db['name']
        size_mb = round(db['sizeOnDisk'] / (1024 * 1024), 2)
        print(f"{name}: {size_mb} MB")
    print("==============================\n")

print_db_sizes()

try:
    client.admin.command('ping')
    print("Pinged your deployment. You successfully connected to MongoDB!")
except Exception as e:
    print(f"Error connecting to MongoDB: {e}")

# ------------ Helpers MongoDB ------------

def get_user_data(guild_id: int, user_id: int):
    doc = points_collection.find_one({"guild_id": guild_id, "user_id": user_id})
    if not doc:
        new_doc = {"guild_id": guild_id, "user_id": user_id, "tp": 0, "mp": 0}
        points_collection.insert_one(new_doc)
        return new_doc
    return doc

def add_points(guild_id: int, user_id: int, category: str, amount: int):
    update = {"$inc": {category: amount}}
    result = points_collection.find_one_and_update(
        {"guild_id": guild_id, "user_id": user_id},
        update,
        upsert=True,
        return_document=True
    )
    if result is None:
        points_collection.insert_one({
            "guild_id": guild_id,
            "user_id": user_id,
            "tp": amount if category == "tp" else 0,
            "mp": amount if category == "mp" else 0,
        })
        return amount
    return result.get(category, 0) + amount

def allowed_roles(guild_id: int) -> List[int]:
    doc = config_collection.find_one({"guild_id": guild_id})
    if doc and "role_ids" in doc:
        return doc["role_ids"]
    return []

def save_allowed_roles(guild_id: int, role_ids: List[int]):
    config_collection.update_one(
        {"guild_id": guild_id},
        {"$set": {"role_ids": role_ids}},
        upsert=True
    )

# ------------ Bot Setup ------------

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def has_permission(member: discord.Member):
    return any(role.id in allowed_roles(member.guild.id) for role in member.roles)


# ------------ /showprofile ------------


@bot.tree.command(name="showprofile", description="Show Training & Mission Points")
@app_commands.describe(member="Member to view; leave empty for yourself")
async def showprofile(interaction: discord.Interaction, member: discord.Member | None = None):
    if member is None:
        await interaction.response.send_message("Member not found.", ephemeral=True)
        return

    guild_id = interaction.guild.id if interaction.guild else 0
    data = get_user_data(guild_id, member.id)
    embed = discord.Embed(title=f"Profile – {member.display_name}")
    embed.add_field(name="Training Points", value=str(data.get("tp", 0)))
    embed.add_field(name="Mission Points", value=str(data.get("mp", 0)))

    await interaction.response.send_message(embed=embed)

    try:
        await asyncio.sleep(15)
        await interaction.delete_original_response()
    except discord.Forbidden:
        pass

POINT_VALUES = {"mvp": 3, "promo": 2, "attended": 1}
MENTION_RE = re.compile(r"<@!?(\d+)>")


# ------------ /addtp ------------


@bot.tree.command(name="addtp", description="Add Training Points with automatic weighting")
@app_commands.describe(
    mvp="Mentions for MVP (+3 each)",
    promo="Mentions for Promo (+2 each)",
    attended="Mentions for Attendance (+1 each)",
    rollcall="Roll‑call message link"
)
async def addtp(
    interaction: discord.Interaction,
    promo: str,
    rollcall: str,
    mvp: str = "",
    attended: str = "",
):
    member = interaction.user
    if not isinstance(member, discord.Member) and interaction.guild:
        member = interaction.guild.get_member(interaction.user.id)
    if member is None:
        await interaction.response.send_message("❌ Cannot check permissions: member not found.", ephemeral=True)
        return

    member = cast(discord.Member, member)
    if not has_permission(member):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ This command can only be used inside a server.", ephemeral=True)
        return

    summary_lines = []

    for field, text in {"mvp": mvp, "promo": promo, "attended": attended}.items():
        if not text:
            continue
        ids = MENTION_RE.findall(text)
        for uid in ids:
            member_to_add = guild.get_member(int(uid))
            if member_to_add is None:
                continue
            new_total = add_points(guild.id, member_to_add.id, "tp", POINT_VALUES[field])
            summary_lines.append(f"{member_to_add.mention} +{POINT_VALUES[field]} TP → **{new_total}**")

    if not summary_lines:
        await interaction.followup.send("ℹ️ No valid mentions found.")
        return

    response = "\n".join(summary_lines)
    if rollcall:
        response += f"\n🔗 Roll‑call: {rollcall}"

    msg = await interaction.followup.send(response)

    if msg is not None:
        await asyncio.sleep(10)
        try:
            await msg.delete()
        except discord.Forbidden:
            pass


# ------------ /addmp ------------


@bot.tree.command(name="addmp", description="Add Mission Points to a member")
@app_commands.describe(
    member="Member to receive points",
    missionpoints="Number of points",
    rollcall="Roll‑call link"
)
async def addmp(
    interaction: discord.Interaction,
    member: discord.Member,
    missionpoints: app_commands.Range[int, 1],
    rollcall: str,
):
    caller = interaction.user
    if not isinstance(caller, discord.Member) and interaction.guild:
        caller = interaction.guild.get_member(interaction.user.id)
    if caller is None:
        await interaction.response.send_message("❌ Cannot check permissions: member not found.", ephemeral=True)
        return

    caller = cast(discord.Member, caller)
    if not has_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ This command can only be used inside a server.", ephemeral=True)
        return

    new_total = add_points(guild.id, member.id, "mp", missionpoints)
    content = f"{member.mention} +{missionpoints} MP → **{new_total}**"
    if rollcall:
        content += f"\n🔗 Roll‑call: {rollcall}"

    await interaction.response.defer(ephemeral=False)
    msg = await interaction.followup.send(content)

    if msg is not None:
        await asyncio.sleep(10)
        try:
            await msg.delete()
        except discord.Forbidden:
            pass


# ------------ /removetp ------------


@bot.tree.command(name="removetp", description="Remove Training Points from mentioned members")
@app_commands.describe(
    member="Members to remove points from (mention one or more)",
    points="Number of training points to remove from each member"
)
async def removetp(
    interaction: discord.Interaction,
    member: str,
    points: app_commands.Range[int, 1],
):
    caller = interaction.user
    if not isinstance(caller, discord.Member) and interaction.guild:
        caller = interaction.guild.get_member(interaction.user.id)
    if caller is None:
        await interaction.response.send_message("❌ Cannot check permissions: member not found.", ephemeral=True)
        return

    caller = cast(discord.Member, caller)
    if not has_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ This command can only be used inside a server.", ephemeral=True)
        return

    ids = MENTION_RE.findall(member)
    if not ids:
        await interaction.followup.send("❌ No valid member mentions found.", ephemeral=True)
        return

    summary_lines = []
    for uid in ids:
        member_to_remove = guild.get_member(int(uid))
        if member_to_remove is None:
            continue
        new_total = add_points(guild.id, member_to_remove.id, "tp", -points)
        summary_lines.append(f"{member_to_remove.mention} -{points} TP → **{new_total}**")

    response = "\n".join(summary_lines)
    msg = await interaction.followup.send(response)

    if msg is not None:
        await asyncio.sleep(10)
        try:
            await msg.delete()
        except discord.Forbidden:
            pass


# ------------ /removemp ------------


@bot.tree.command(name="removemp", description="Remove Mission Points from a member")
@app_commands.describe(member="Member to remove points from", missionpoints="Number of points to remove")
async def removemp(
    interaction: discord.Interaction,
    member: discord.Member,
    missionpoints: app_commands.Range[int, 1],
):
    caller = interaction.user
    if not isinstance(caller, discord.Member) and interaction.guild:
        caller = interaction.guild.get_member(interaction.user.id)
    if caller is None:
        await interaction.response.send_message("❌ Cannot check permissions: member not found.", ephemeral=True)
        return

    caller = cast(discord.Member, caller)
    if not has_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ This command can only be used inside a server.", ephemeral=True)
        return

    new_total = add_points(guild.id, member.id, "mp", -missionpoints)
    content = f"{member.mention} -{missionpoints} MP → **{new_total}**"

    await interaction.response.defer(ephemeral=False)
    msg = await interaction.followup.send(content)

    if msg is not None:
        await asyncio.sleep(10)
        try:
            await msg.delete()
        except discord.Forbidden:
            pass

# ------------ Setup Group ------------

class Setup(app_commands.Group, name="setup", description="Configure roles allowed to add points"):

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member) and interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message("❌ Cannot verify permissions.", ephemeral=True)
            return False
        member = cast(discord.Member, member)
        if not member.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="addrole", description="Authorize a role")
    async def addrole(self, interaction: discord.Interaction, role: discord.Role):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Command can only be used in a server.", ephemeral=True)
            return

        roles = allowed_roles(guild.id)
        if role.id not in roles:
            roles.append(role.id)
            save_allowed_roles(guild.id, roles)
            await interaction.response.send_message(f"✅ {role.mention} authorized.")
        else:
            await interaction.response.send_message(f"ℹ️ {role.mention} already authorized.", ephemeral=True)
           
# ------------ /removerole ------------
   
    @app_commands.command(name="removerole", description="Remove a role from authorization list")
    async def removerole(self, interaction: discord.Interaction, role: discord.Role):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Command can only be used in a server.", ephemeral=True)
            return

        roles = allowed_roles(guild.id)
        if role.id in roles:
            roles.remove(role.id)
            save_allowed_roles(guild.id, roles)
            await interaction.response.send_message(f"✅ {role.mention} removed.")
        else:
            await interaction.response.send_message(f"ℹ️ {role.mention} was not in the list.", ephemeral=True)
           
# ------------ /list ------------
   
    @app_commands.command(name="list", description="Show authorized roles")
    async def listroles(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Command can only be used in a server.", ephemeral=True)
            return

        ids = allowed_roles(guild.id)
        if not ids:
            await interaction.response.send_message("🔸 No authorized roles.", ephemeral=True)
            return

        mentions = []
        for rid in ids:
            role = guild.get_role(rid)
            if role is not None:
                mentions.append(role.mention)

        await interaction.response.send_message("🔸 Authorized roles:\n" + "\n".join(mentions))

bot.tree.add_command(Setup())

# ------------ Events ------------

@bot.event
async def on_ready():
    user = bot.user
    if user is None:
        print("Bot user is None, something went wrong.")
        return

    print(f"Logged in as {user} (ID: {user.id})")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as e:
        print("Slash-command sync error:", e)

# ------------ Keep-alive server ------------

app = Flask("")

@app.route("/", methods=["GET", "HEAD"])
def home():
    # Para GET devolvemos texto; para HEAD Flask enviará solo cabeceras 200 OK
    return "Bot is running!", 200
   
def run_web():
    app.run(host="0.0.0.0", port=8080, debug=False)

Thread(target=run_web, daemon=True).start()

# ------------ Auto‑restart checker ------------

def auto_restart_check():
    while True:
        time.sleep(300)  # 300 segundos = 5 minutos
        if not bot.is_closed() or not bot.is_ready():
            print("❌ Bot no está listo. Reiniciando...")
            os._exit(1)  # Render reiniciará automáticamente
        else:
            print("✅ Bot verificado correctamente.") 

threading.Thread(target=auto_restart_check, daemon=True).start()

# ------------ Run Bot ------------

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Environment variable DISCORD_TOKEN not set.")

bot.run(TOKEN)
