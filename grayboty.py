# ─────────────── Imports ───────────────
import os
import re
import sys
import time
import logging
import threading
import traceback
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

@bot.event
async def on_error(event, *args, **kwargs):
    import traceback
    print(f"[GLOBAL ERROR] in event: {event}")
    traceback.print_exc()

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    print(f"[COMMAND ERROR] {interaction.command.name if interaction.command else 'Unknown'}: {error}")
    with contextlib.suppress(Exception):
        await interaction.followup.send(
            "⚠️ An error occurred while executing the command.",
            ephemeral=True
        )

# ───────────── RANK SYSTEM ─────────────
rank_list = [
    "Initiate", "Acolyte", "Disciple", "Seeker", "Knight", "Gray Knight",
    "Silver Knight", "Master - On trial", "Grandmaster", "Master of Balance",
    "Gray Lord", "Ashen Lord", "Gray Emperor", "Elder Gray Emperor"
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
    "Gray Emperor": "<:GrayEmp:1429396732269035622>",
    "Elder Gray Emperor": "<:ElderEmp:1429396655085715498>",
}
def get_highest_rank(member: discord.Member) -> str:
    member_roles = [role.name for role in member.roles]
    ranks_found = [rank for rank in rank_list if rank in member_roles]
    if not ranks_found:
        return "No Rank"
    highest_rank = max(ranks_found, key=lambda r: rank_list.index(r))
    emoji = rank_emojis.get(highest_rank, "")
    return f"{emoji} | {highest_rank}" if emoji else highest_rank

# ───────────── REQUISITOS DE RANGO ─────────────
rank_requirements = {
    "Acolyte": {"tp": 1},
    "Disciple": {"tp": 5, "mp": 3, "tier": "Low-Tier"},
    "Seeker": {"tp": 12, "mp": 5, "tier": "Low-Tier"},
    "Knight": {"tp": 18, "mp": 9, "tier": "Middle-Tier"},
    "Gray Knight": {"tp": 24, "mp": 12, "tier": "Middle-Tier [ ⁑ ]"},
    "Silver Knight": {"tp": 30, "mp": 18, "tier": "Middle-Tier [ ⁂ ]"}
}

# ───────────── MEDALLAS DE HONOR ─────────────
medal_roles = {
    1394844735553929397: "<:tgohonor:1394844480258965664>",
    1381442617916657715: "<:moihonor:1394844519589216357>",
    1381449456205037698: "<:clxphonor:1394844536022237244>",
    1447669416061046805: "<:maskhonor:1454202929866080319>",
    1394844722828152874: "<:michishonor:1394844563499122728>",
    1394833159215906858: "<:ashenhonor:1394844598928670810>",
    1394833210218643496: "<:grayhonor:1394844626661277736>",
}

# ───────────── ROLES DE LEVEL-TIER ─────────────
tier_roles = {
    "✩ Legend-Tier": 1383883524783996998,
    "★ Ashenlight-Tier": 1383882778474578080,
    "Celestial-Tier": 1383882358343733330,
    "Elite-Tier": 1383878896578986086,
    "High-Tier": 1383020440993271839,
    "Middle-Tier": 1383020382071820328,
    "Low-Tier": 1383019912762626108,
    "[ ⁂ ]": 1384516311354445965,
    "[ ⁑ ]": 1384186134891855872,
}

# ───────────── EMOJIS DE LEVEL-TIER ─────────────
tier_emojis = {
    "✩ Legend-Tier": "<:LegendTier:1398285409372606484>",
    "★ Ashenlight-Tier": "<:AshenTier:1398285363218485409>",
    "Celestial-Tier": "<:CelestialTier:1398285318351880253>",
    "Elite-Tier": "<:EliteTier:1398285285611012157>",
    "High-Tier": "<:HighTier:1398285236705431574>",
    "Middle-Tier": "<:MiddleTier:1398285202257739796>",
    "Low-Tier": "<:LowTier:1398285157101736058>"
}

# ───────────── ORDEN RANGOS GRUPO ─────────────
group_ranks_order = [
    "Elder Gray Emperor",
    "Gray Emperor",
    "Ashen Lord",
    "Gray Lord",
    "Master of Balance",
    "Grandmaster",
    "Master - On trial",
    "Silver Knight",
    "Gray Knight",
    "Knight",
    "Seeker",
    "Disciple",
    "Acolyte",
    "Initiate",
]

# ───────────── /SHOWPROFILE ─────────────
@bot.tree.command(name="showprofile", description="Show Training & Mission Points")
@app_commands.describe(member="Member to view; leave empty for yourself")
async def showprofile(interaction: discord.Interaction, member: discord.Member | None = None):

    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True
        )
        return

    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True)

    if member is None:
        member = interaction.user

    doc = points_collection.find_one({
        "guild_id": interaction.guild.id,
        "user_id": member.id
    })

    if not doc or all(doc.get(k, 0) == 0 for k in ("tp", "mp", "rp", "wp")):
        safe_name = member.display_name.replace("_", "\\_")
        msg = await interaction.followup.send(
            f"_**{safe_name}** has not yet woven their story into this place._"
        )
        await asyncio.sleep(15)
        with contextlib.suppress(discord.Forbidden, discord.NotFound):
            await msg.delete()
        return

    highest_rank_raw = get_highest_rank(member)
    current_rank = (
        highest_rank_raw.split("|")[-1].strip()
        if highest_rank_raw and "|" in highest_rank_raw
        else highest_rank_raw
    )

    embed = discord.Embed(
        title=f"{member.display_name}",
        color=discord.Color.from_rgb(247, 240, 172)
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(name="**Training Points**", value=doc.get("tp", 0), inline=True)
    embed.add_field(name="**Mission Points**", value=doc.get("mp", 0), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="**War Points**", value=doc.get("wp", 0), inline=True)
    embed.add_field(name="**Raid Points**", value=doc.get("rp", 0), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(
        name="",
        value="<:H1Laser:1395749428135985333><:H2Laser:1395749449753563209>"
              "<:R1Laser:1395746456681578628><:R1Laser:1395746456681578628>"
              "<:R1Laser:1395746456681578628><:R1Laser:1395746456681578628>"
              "<:R2Laser:1395746474293198949> "
              "<:M2LaserInv:1395909504482283750><:M1Laser:1395909456986112110>"
              "<:M1Laser:1395909456986112110><:M1Laser:1395909456986112110>"
              "<:M1Laser:1395909456986112110><:H2LaserInv:1395909361494396948>"
              "<:H1LaserInv:1395909332339790065>",
        inline=False
    )
    # Medallas
    glory_emoji = "<:Glory:1401695802660749362>"
    user_medals_full = []

    LEADER = 1419415839471304856

    if discord.utils.get(member.roles, id=LEADER):
        user_medals_full = list(medal_roles.values())
    else:
        for role_id, emoji in medal_roles.items():
            if discord.utils.get(member.roles, id=role_id):
                user_medals_full.append(emoji)
            else:
                user_medals_full.append(glory_emoji)

    embed.add_field(
        name="**Medals of honor**",
        value=" {} ".format(" ┃ ".join(user_medals_full)),
        inline=False
    )
    # Retired roles
    retired_roles = [
        {
            "id": 1413828641397149716,
            "name": "Emeritus Emperor",
            "subtitle": "\u200b",
            "emoji": "<:RetiredTGO:1429142210301005904>",
            "text": "Once crowned, forever eternal."
        },
        {
            "id": 1413829540987277332,
            "name": "Elder of Council",
            "subtitle": "\u200b",
            "emoji": "<:RetiredCo:1413856505987596380>",
            "text": "Their wisdom echoes in every council hall."
        },
        {
            "id": 1381562883803971605,
            "name": "Retired",
            "subtitle": "\u200b",
            "emoji": "<:RetiredHR:1413856468595114056>",
            "text": "Their honor endures beyond their service."
        }
    ]

    retired_detected = None
    for role in retired_roles:
        if discord.utils.get(member.roles, id=role["id"]):
            retired_detected = role
            break

    if retired_detected:
        embed.add_field(
            name="**Rank**",
            value=f"{retired_detected['emoji']} {retired_detected['name']}\n-# _{retired_detected['subtitle']}_",
            inline=False
        )
    else:
        embed.add_field(
            name="**Rank**",
            value=f"{rank_emojis.get(current_rank, '')} | {current_rank}",
            inline=False
        )
    # Level-Tier
    member_role_ids = [role.id for role in member.roles]
    level_tier = None
    stars = ""

    for base in [
        "✩ Legend-Tier", "★ Ashenlight-Tier", "Celestial-Tier",
        "Elite-Tier", "High-Tier", "Middle-Tier", "Low-Tier"
    ]:
        if tier_roles.get(base) in member_role_ids:
            level_tier = base
            break

    if level_tier:
        if tier_roles.get("[ ⁂ ]") in member_role_ids:
            stars = " [ ⁂ ]"
        elif tier_roles.get("[ ⁑ ]") in member_role_ids:
            stars = " [ ⁑ ]"

        embed.add_field(
            name="**Level-Tier**",
            value=f"{tier_emojis.get(level_tier, '')} {level_tier}{stars}",
            inline=False
        )
    # Texto final narrativo (INTACTO)
    if retired_detected:
        embed.add_field(name="", value=f"> {retired_detected['text']}", inline=False)

    elif current_rank == "Elder Gray Emperor":
        embed.add_field(
            name="",
            value="> Founder, Owner and Emperor of The Grey Order",
            inline=False
        )

    elif current_rank == "Gray Emperor":
        embed.add_field(
            name="",
            value="> Leader and Emperor of the Grey Order",
            inline=False
        )

    elif current_rank in ["Gray Lord", "Ashen Lord"]:
        embed.add_field(
            name="",
            value="> Part of the council of The Grey Order",
            inline=False
        )

    elif current_rank in ["Silver Knight", "Master - On trial", "Grandmaster", "Master of Balance"]:
        embed.add_field(
            name="",
            value="From this rank onwards, promotions are decided by HR.",
            inline=False
        )

    else:
        if current_rank in rank_list:
            idx = rank_list.index(current_rank)
            if idx + 1 < len(rank_list):
                next_rank = rank_list[idx + 1]
                if next_rank in rank_requirements:
                    req = rank_requirements[next_rank]
                    req_text = (
                        f"**Next rank**\n{rank_emojis.get(next_rank, '')} | {next_rank}\n"
                        f"· _**{req.get('tp', 0)}** training points_\n"
                        f"· _**{req.get('mp', 0)}** mission points_"
                    )
                    if req.get("tier"):
                        req_text += f"\n· _**{req['tier']}** level_"
                    embed.add_field(name="", value=req_text, inline=False)

    embed.add_field(
        name="",
        value="-# <:OficialTGO:1395904116072648764> The Gray Order",
        inline=False
    )

    msg = await interaction.followup.send(embed=embed)

    await asyncio.sleep(30)
    with contextlib.suppress(discord.Forbidden, discord.NotFound):
        await msg.delete()

# ───────────── /LOGS ─────────────
LOG_CHANNEL_ID = 1398432802281750639  # Hidden channel for logs

async def log_command_use(interaction: discord.Interaction):
    params = []

    if interaction.data.get("options"):
        for option in interaction.data["options"]:
            name = option.get("name")
            value = option.get("value")

            try:
                if name == "member":
                    user = await interaction.guild.fetch_member(int(value))
                    display_value = f"{user.mention} ({user})"
                elif name == "level":
                    role = interaction.guild.get_role(int(value))
                    display_value = role.mention if role else f"ID:{value}"
                else:
                    display_value = str(value)
            except Exception:
                display_value = f"ID:{value}"

            params.append(f"{name}: {display_value}")

    params_text = "\n".join(params) if params else "*No arguments*"

    embed = discord.Embed(
        title="📜 Command Log",
        description=f"{interaction.user.mention} has executed the command **/{interaction.command.name}**:\n{params_text}",
        color=0x999999,
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"User ID: {interaction.user.id}")

    log_channel = interaction.client.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        try:
            await log_channel.send(embed=embed)
        except Exception:
            pass  # Silenciar cualquier error al enviar logs

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

    if not has_basic_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return
    rollcall = rollcall.strip()
    if rollcall and "discord" not in rollcall:
        await interaction.response.send_message("❌ Invalid roll‑call link format.", ephemeral=True)
        return
    await log_command_use(interaction)
    await interaction.response.defer()
    guild = interaction.guild
    # Añadir +1 TP al ejecutor si tiene permiso (internamente, no visible en embed)
    add_points(guild.id, caller.id, "tp", 1)

    embed_description = [f"{caller.mention} has added training points to:"]
    any_valid_mentions = False
    # Preparamos un diccionario por cantidad de TP para batch
    batch_additions: dict[int, list[int]] = {}  # {points: [member_id,...]}

    for cat, text in {"mvp": mvp, "promo": promo, "attended": attended}.items():
        pts_to_add = POINT_VALUES.get(cat, 0)
        if pts_to_add <= 0:
            continue
        for uid in MENTION_RE.findall(text):
            member = guild.get_member(int(uid))
            if member is not None:
                any_valid_mentions = True
                batch_additions.setdefault(pts_to_add, []).append(member.id)
                embed_description.append(f"{member.mention} +{pts_to_add} TP")
    if not any_valid_mentions:
        await interaction.followup.send("ℹ️ No valid member mentions found.")
        return

    for pts, member_ids in batch_additions.items():
        add_points_batch(guild.id, member_ids, "tp", pts)  # batch
    if rollcall:
        embed_description.append(f"\n🔗 Rollcall: {rollcall}")
    embed = discord.Embed(
        title="Training Points Added",
        description="\n".join(embed_description),
        color=discord.Color.green()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(20)
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
    if not has_basic_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return
    # Validar que missionpoints no supere 4
    if missionpoints > 4:
        await interaction.response.send_message("❌ You cannot add more than 4 Mission Points with this command.", ephemeral=True)
        return
        
    rollcall = rollcall.strip()
    if rollcall and "discord" not in rollcall:
        await interaction.response.send_message("❌ Invalid roll‑call link format.", ephemeral=True)
        return
        
    await log_command_use(interaction)  # logs
    await interaction.response.defer()
    # Solo añadir puntos si missionpoints es mayor que 0
    if missionpoints > 0:
        add_points(interaction.guild.id, member.id, "mp", missionpoints)
    # Construir descripción del embed
    embed_description = [f"{caller.mention} has added mission points to {member.mention}:"]
    embed_description.append(f"{member.mention} +{missionpoints} MP")
    embed_description.append(f"\n🔗 Rollcall: {rollcall}")

    embed = discord.Embed(
        title="Mission Points Added",
        description="\n".join(embed_description),
        color=discord.Color.blue()
    )

    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(20)
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
        await msg.delete()
       
# ───────────── /addra ─────────────
@bot.tree.command(name="addra", description="Add Raid Points (Rp) and Mission Points (Mp)")
@app_commands.describe(
    members="Members to receive Raid Points",
    rollcall="Roll‑call message link",
    extra="Extra members to receive +1 Mission Point (optional)",
)
async def addra(
    interaction: discord.Interaction,
    members: str,
    rollcall: str,
    extra: str | None = None,
):
    caller = cast(discord.Member, interaction.user)
    if not has_basic_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return
    rollcall = rollcall.strip()
    if rollcall and "discord" not in rollcall:
        await interaction.response.send_message("❌ Invalid roll‑call link format.", ephemeral=True)
        return
    member_ids = [int(mid) for mid in MENTION_RE.findall(members)]
    if not member_ids:
        await interaction.response.send_message("❌ No valid member mentions found in members.", ephemeral=True)
        return
    extra_ids = [int(eid) for eid in MENTION_RE.findall(extra)] if extra else []
    await log_command_use(interaction)
    await interaction.response.defer()

    guild = interaction.guild
    summary = []
    # BATCH: obtener todos los miembros de members
    members_objs = {m.id: m for m in guild.members if m.id in member_ids}
    for mid in member_ids:
        member = members_objs.get(mid)
        if member:
            summary.append(f"{member.mention} +1 Rp")
        else:
            summary.append(f"User ID {mid} not found in guild.")
    # Llamada a DB en batch
    add_points_batch(guild.id, list(members_objs.keys()), "rp", 1)
    # BATCH: extra MPs
    extra_objs = {m.id: m for m in guild.members if m.id in extra_ids}
    for eid in extra_ids:
        extra_member = extra_objs.get(eid)
        if extra_member:
            summary.append(f"{extra_member.mention} +1 Mp (extra)")
        else:
            summary.append(f"User ID {eid} not found in guild.")
    if extra_objs:
        add_points_batch(guild.id, list(extra_objs.keys()), "mp", 1)
    # Crear embed con lista de participantes y rollcall
    embed = discord.Embed(
        title="Raid Points Added",
        description="\n".join(summary) + (f"\n🔗 {rollcall}" if rollcall else ""),
        color=discord.Color.dark_gold()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(20)
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
        await msg.delete()

# ───────────── /addwar ─────────────
@bot.tree.command(name="addwar", description="Add War Points (Wp) to multiple members")
@app_commands.describe(
    members="Members to receive War Points",
    rollcall="Roll‑call message link",
)
async def addwar(
    interaction: discord.Interaction,
    members: str,
    rollcall: str,
):
    caller = cast(discord.Member, interaction.user)
    if not has_basic_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return
    rollcall = rollcall.strip()
    if rollcall and "discord" not in rollcall:
        await interaction.response.send_message("❌ Invalid roll‑call link format.", ephemeral=True)
        return
    member_ids = [int(mid) for mid in MENTION_RE.findall(members)]
    if not member_ids:
        await interaction.response.send_message("❌ No valid member mentions found in members.", ephemeral=True)
        return
    await log_command_use(interaction)
    await interaction.response.defer()

    guild = interaction.guild
    summary = []
    members_objs = {m.id: m for m in guild.members if m.id in member_ids}
    for mid in member_ids:
        member = members_objs.get(mid)
        if member:
            summary.append(f"{member.mention} +1 Wp")
        else:
            summary.append(f"User ID {mid} not found in guild.")
    if members_objs:
        add_points_batch(guild.id, list(members_objs.keys()), "wp", 1)

    embed = discord.Embed(
        title="War Points Added",
        description="\n".join(summary) + (f"\n🔗 {rollcall}" if rollcall else ""),
        color=discord.Color.purple()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(20)
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
        await msg.delete()

# ───────────── /addeve ─────────────
@bot.tree.command(name="addeve", description="Add Event Points (Eve) and Mission Points (MP) to multiple members")
@app_commands.describe(
    members="Members to receive Event and Mission Points (only mentions allowed)",
    rollcall="Roll-call message link"
)
async def addeve(
    interaction: discord.Interaction,
    members: str,
    rollcall: str,
):
    caller = cast(discord.Member, interaction.user)
    if not has_basic_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return
    rollcall = rollcall.strip()
    if rollcall and "discord" not in rollcall:
        await interaction.response.send_message("❌ Invalid roll‑call link format.", ephemeral=True)
        return
    member_ids = [int(mid) for mid in MENTION_RE.findall(members)]
    if not member_ids:
        await interaction.response.send_message("❌ No valid member mentions found in members.", ephemeral=True)
        return

    await log_command_use(interaction)
    await interaction.response.defer()

    guild = interaction.guild
    summary = []
    # Obtener todos los miembros válidos en batch
    members_objs = {m.id: m for m in guild.members if m.id in member_ids}
    for mid in member_ids:
        member = members_objs.get(mid)
        if member:
            summary.append(f"{member.mention} +1 Eve, +1 MP")
        else:
            summary.append(f"User ID {mid} not found in guild.")
    # Aplicar puntos en batch
    if members_objs:
        add_points_batch(guild.id, list(members_objs.keys()), "eve", 1)
        add_points_batch(guild.id, list(members_objs.keys()), "mp", 1)

    embed = discord.Embed(
        title="Event and Mission Points Added",
        description="\n".join(summary) + (f"\n🔗 {rollcall}" if rollcall else ""),
        color=discord.Color.gold()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(20)
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
        await msg.delete()

# ───────────── /addtier ─────────────
@bot.tree.command(name="addtier", description="Set or update a member's tier level")
@app_commands.describe(
    member="Member to assign tier",
    level="Tier role to assign (mention the role)",
    stars="Stars level, 2 or 3 (optional)",
    rollcall="Roll‑call link",
)
async def addtier(
    interaction: discord.Interaction,
    member: discord.Member,
    level: discord.Role,
    rollcall: str,
    stars: app_commands.Range[int, 2, 3] | None = None,
):
    caller = cast(discord.Member, interaction.user)
    if not has_basic_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return

    if not rollcall.strip().startswith("https://discord.com"):
        await interaction.response.send_message("❌ Invalid roll‑call link format.", ephemeral=True)
        return

    await log_command_use(interaction)
    await interaction.response.defer()

    level_name = level.name
    tier_role_id = tier_roles.get(level_name)

    if not tier_role_id:
        msg = await interaction.followup.send("❌ Invalid tier level. Make sure you provide a valid Tier.")
        await asyncio.sleep(20)
        with contextlib.suppress((discord.Forbidden, discord.NotFound)):
            await msg.delete()
        return

    valid_star_levels = {"Low-Tier", "Middle-Tier", "High-Tier", "Elite-Tier", "Celestial-Tier"}
    if stars and level_name not in valid_star_levels:
        msg = await interaction.followup.send("❌ Only Low-Tier, Middle-Tier, High-Tier, Elite-Tier and Celestial-Tier can receive stars.")
        await asyncio.sleep(15)
        with contextlib.suppress((discord.Forbidden, discord.NotFound)):
            await msg.delete()
        return
    # Eliminar Tiers anteriores (incluyendo estrellas)
    roles_to_remove = []
    for rid in tier_roles.values():
        role_obj = discord.utils.get(interaction.guild.roles, id=rid)
        if role_obj and role_obj in member.roles:
            roles_to_remove.append(role_obj)

    if roles_to_remove:
        await member.remove_roles(*roles_to_remove)
    # Añadir nuevo Tier
    new_tier_role = discord.utils.get(interaction.guild.roles, id=tier_role_id)
    added_roles = []

    if new_tier_role:
        await member.add_roles(new_tier_role)
        added_roles.append(new_tier_role.mention)
    # Añadir estrella si procede
    if stars:
        star_label = "[ ⁑ ]" if stars == 2 else "[ ⁂ ]"
        star_role_id = tier_roles.get(star_label)
        star_role = discord.utils.get(interaction.guild.roles, id=star_role_id)
        if star_role:
            await member.add_roles(star_role)
            added_roles.append(star_role.mention)

    embed = discord.Embed(
        title="🎇 Tier Updated",
        description=(
            f"{member.mention} has been assigned the Tier: **{level_name}**"
            + (f"\nStars: **{stars}**" if stars else "")
            + f"\nRoles given: {' | '.join(added_roles)}"
            + f"\n🔗 {rollcall}"
        ),
        color=discord.Color.from_rgb(141, 228, 212)
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(20)
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
        await msg.delete()

# ───────────── /tierList -updated ─────────────
import asyncio

tierlist_locks: dict[str, asyncio.Lock] = {}

class TierListView(discord.ui.View):
    def __init__(self, pages: list[list[str]], invoker_pos: int | None, filter_name: str | None = None):
        super().__init__(timeout=180) 
        self.pages = pages
        self.current_page = 0
        self.invoker_pos = invoker_pos
        self.filter_name = filter_name
        self.message: discord.Message | None = None
        self._lock: asyncio.Lock | None = None  # Lock por vista

    async def _get_lock(self):
        if not self._lock:
            self._lock = tierlist_locks.setdefault(str(id(self)), asyncio.Lock())
        return self._lock

    async def send_initial(self, interaction: discord.Interaction):
        embed = self.create_embed()
        self.message = await interaction.edit_original_response(embed=embed, view=self)

    def create_embed(self):
        filter_text = f"\n 🔎 Filter applied: {self.filter_name}" if self.filter_name else ""

        tier_colors = {
            "✩ Legend-Tier": 0xebb9ff,
            "★ Ashenlight-Tier": 0x8d4747,
            "Celestial-Tier": 0x5583e7,
            "Elite-Tier": 0x77d8a0,
            "High-Tier": 0xc29b38,
            "Middle-Tier": 0xd8dada,
            "Low-Tier": 0x837373,
        }
        color = tier_colors.get(self.filter_name, 0xffffff)
        # Truncar líneas demasiado largas y embed < 6000 caracteres
        page_content = []
        for line in self.pages[self.current_page]:
            if len(line) > 100:  # Limitar cada línea
                line = line[:97] + "..."
            page_content.append(line)
        page_text = "\n".join(page_content)

        embed = discord.Embed(
            title="",
            description=(
                "# 🏆 TIER LEADERBOARD\n"
                f"{filter_text}\n"
                "-# ─────────────────────────\n"
                + "\n".join(self.pages[self.current_page])
            ),
            color=color
        )
        footer_text = f"Page {self.current_page + 1}/{len(self.pages)}"
        if self.invoker_pos:
            embed.set_footer(text=f"Your position is: {self.invoker_pos}\n{footer_text}")
        else:
            embed.set_footer(text=f"You have no Tier position.\n{footer_text}")
        return embed

    async def update(self, interaction: discord.Interaction):
        if not self.message:
            return
        embed = self.create_embed()
        lock = await self._get_lock()
        async with lock:
            # Solo actualizar si hay cambio
            if self.message.embeds and self.message.embeds[0].description == embed.description:
                await interaction.response.defer()  # Evitar errores de "already responded"
                return
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⏮️ First", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 0
        await self.update(interaction)

    @discord.ui.button(label="⬅️ Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await self.update(interaction)

    @discord.ui.button(label="Go to You", style=discord.ButtonStyle.success, emoji="🎯")
    async def go_to_you(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.invoker_pos is None:
            await interaction.response.send_message("You have no Tier position.", ephemeral=True)
            return
        per_page = 15
        self.current_page = (self.invoker_pos - 1) // per_page
        await self.update(interaction)

    @discord.ui.button(label="➡️ Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < len(self.pages) - 1:
            self.current_page += 1
            await self.update(interaction)

    @discord.ui.button(label="⏭️ Last", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = len(self.pages) - 1
        await self.update(interaction)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.delete()
            except discord.NotFound:
                pass

@bot.tree.command(name="tierlist", description="Show top members sorted by Tier and group rank")
@app_commands.describe(tier="Optional: filter by a specific Tier")
@app_commands.choices(tier=[
    app_commands.Choice(name="✩ Legend-Tier", value="✩ Legend-Tier"),
    app_commands.Choice(name="★ Ashenlight-Tier", value="★ Ashenlight-Tier"),
    app_commands.Choice(name="Celestial-Tier", value="Celestial-Tier"),
    app_commands.Choice(name="Elite-Tier", value="Elite-Tier"),
    app_commands.Choice(name="High-Tier", value="High-Tier"),
    app_commands.Choice(name="Middle-Tier", value="Middle-Tier"),
    app_commands.Choice(name="Low-Tier", value="Low-Tier"),
])
async def tierlist(interaction: discord.Interaction, tier: app_commands.Choice[str] | None = None):
    await interaction.response.defer(thinking=True)

    guild = interaction.guild
    members = guild.members

    tier_order = [
        "✩ Legend-Tier", "★ Ashenlight-Tier", "Celestial-Tier", "Elite-Tier",
        "High-Tier", "Middle-Tier", "Low-Tier"
    ]

    def get_member_tier(member: discord.Member) -> str | None:
        role_ids = {role.id for role in member.roles}
        base_tier = None
        for t in tier_order:
            if tier_roles.get(t) in role_ids:
                base_tier = t
                break
        if not base_tier:
            return None
        if tier_roles.get("[ ⁂ ]") in role_ids:
            return f"{base_tier} [ ⁂ ]"
        elif tier_roles.get("[ ⁑ ]") in role_ids:
            return f"{base_tier} [ ⁑ ]"
        else:
            return base_tier

    def get_member_rank(member: discord.Member) -> str | None:
        member_role_names = {role.name for role in member.roles}
        for rank in group_ranks_order:
            if rank in member_role_names:
                return rank
        return None

    members_with_tier = []
    for member in members:
        tier_name = get_member_tier(member)
        if tier_name:
            base = tier_name.split(" [")[0].strip()
            if not tier or base == tier.value:
                rank = get_member_rank(member) or "Initiate"
                members_with_tier.append((member, tier_name, rank))

    if not members_with_tier:
        await interaction.edit_original_response(content="No members with Tier roles found.", embed=None)
        return

    def parse_tier_components(tier_name: str):
        stars = 0
        if "[ ⁂ ]" in tier_name:
            stars = 3
        elif "[ ⁑ ]" in tier_name:
            stars = 2
        base = tier_name.split(" [")[0].strip()
        base_index = tier_order.index(base) if base in tier_order else len(tier_order)
        return (base_index, -stars)

    def rank_index(rank_name: str) -> int:
        return group_ranks_order.index(rank_name) if rank_name in group_ranks_order else len(group_ranks_order)

    members_with_tier.sort(key=lambda x: (
        parse_tier_components(x[1]),
        rank_index(x[2])
    ))

    invoker_pos = None
    invoker_id = interaction.user.id
    for i, (member, _, _) in enumerate(members_with_tier, start=1):
        if member.id == invoker_id:
            invoker_pos = i
            break

    lines = []
    for i, (member, tier_name, _) in enumerate(members_with_tier, start=1):
        base_tier = tier_name.split(" [")[0].strip()
        emoji = tier_emojis.get(base_tier, "")
        name = member.display_name
        if i == 1:
            line = f"{str(i).rjust(2)}. {emoji} 🥇 __**TOP 1**__ » {name} — {tier_name}"
        elif i == 2:
            line = f"{str(i).rjust(2)}. {emoji} 🥈 __**TOP 2**__ » {name} — {tier_name}"
        elif i == 3:
            line = f"{str(i).rjust(2)}. {emoji} 🥉 __**TOP 3**__ » {name} — {tier_name}"
        else:
            line = f"{str(i).rjust(2)}. {emoji} {name} — {tier_name}"
        lines.append(line)

    per_page = 15
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)]

    view = TierListView(pages=pages, invoker_pos=invoker_pos, filter_name=tier.value if tier else None)
    await view.send_initial(interaction)

# ───────────── /deltp ─────────────
@bot.tree.command(name="deltp", description="Remove Training Points from one or more members (Admin only)")
@app_commands.describe(
    members="Mentions or IDs of members separated by spaces",
    points="Points to remove (positive integer)"
)
async def deltp(interaction: discord.Interaction, members: str, points: app_commands.Range[int, 1]):

    caller = cast(discord.Member, interaction.user)
    if not has_full_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return

    await interaction.response.defer()
    guild = interaction.guild

    member_ids = MENTION_RE.findall(members)
    if not member_ids:
        await interaction.followup.send("❌ No valid member mentions found.", ephemeral=True)
        return

    summary = []
    found_any = False  # Indicador para saber si al menos un miembro válido fue afectado

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
            found_any = True
        else:
            summary.append(f"{member.mention} has no TP to remove.")

    if found_any:
        await log_command_use(interaction)  # <<== Log solo si se eliminó algún punto

    embed = discord.Embed(
        title="⚠️ Training Points Removed",
        description="\n".join(summary),
        color=discord.Color.orange()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(20)
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
        await msg.delete()

       
# ───────────── /delmp ─────────────
@bot.tree.command(name="delmp", description="Remove Mission Points from one or more members (Admin only)")
@app_commands.describe(
    members="Mentions or IDs of members separated by spaces",
    points="Points to remove (positive integer)"
)
async def delmp(interaction: discord.Interaction, members: str, points: app_commands.Range[int, 1]):

    caller = cast(discord.Member, interaction.user)
    if not has_full_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return

    await interaction.response.defer()
    guild = interaction.guild

    member_ids = MENTION_RE.findall(members)
    if not member_ids:
        await interaction.followup.send("❌ No valid member mentions found.", ephemeral=True)
        return

    summary = []
    found_any = False

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
            found_any = True
        else:
            summary.append(f"{member.mention} has no MP to remove.")

    if found_any:
        await log_command_use(interaction)

    embed = discord.Embed(
        title="⚠️ Mission Points Removed",
        description="\n".join(summary),
        color=discord.Color.orange()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(20)
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
        await msg.delete()


# ───────────── /addall ─────────────
@bot.tree.command(name="addall", description="Add TP and/or MP to one member (Admin only)")
@app_commands.describe(
    member="Member to add points to",
    tp="Training Points to add (optional, default 0)",
    mp="Mission Points to add (optional, default 0)"
)
async def addall(
    interaction: discord.Interaction,
    member: discord.Member,
    tp: app_commands.Range[int, 0] = 0,
    mp: app_commands.Range[int, 0] = 0,
):
    caller = cast(discord.Member, interaction.user)
    if not has_full_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return

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

    await log_command_use(interaction)

    embed = discord.Embed(
        title="✅ Points Added",
        description="\n".join(results),
        color=discord.Color.yellow()
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(20)
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
        await msg.delete()


# ───────────── Autorización de roles ─────────────
BASIC_ROLE_IDS = {
    1381235438563491841,  # LEADERSHIP
    1399751111602212884,  # Gray Council
    1381244026790871111,  # TGO | Staff
}
FULL_ROLE_IDS = {
    1381235438563491841,  # LEADERSHIP
    1399751111602212884,  # Gray Council
}
def has_permission(member: discord.Member, allowed_roles: set[int]) -> bool:
    """Check if member has at least one role in allowed_roles."""
    return any(role.id in allowed_roles for role in member.roles)
has_basic_permission = lambda m: has_permission(m, BASIC_ROLE_IDS)
has_full_permission = lambda m: has_permission(m, FULL_ROLE_IDS)

# ───────────── Eventos ─────────────
monitor_lock = threading.Lock()  # Lock global para reinicio
@bot.event
async def on_ready():
    print(f"🤖 Bot started as {bot.user} (ID: {bot.user.id}) — connected successfully.")
    if not hasattr(bot, "synced"):
        try:
            synced = await bot.tree.sync()
            print(f"☑️ Synced {len(synced)} slash commands.")
            bot.synced = True
        except Exception as e:
            print(f"❌ Slash command sync error: {e}")
    threading.Thread(target=monitor_bot, daemon=True).start()

# ───────────── Keep‑alive server ─────────────
app = Flask(__name__)
@app.route("/", methods=["GET", "HEAD"])
def home():
    return "Bot is running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
threading.Thread(target=run_flask, daemon=True).start()

# ─────── Monitor Bot Mejorado ────────
def monitor_bot():
    print("⏳ Waiting 5 minutes before starting monitoring…", flush=True)
    time.sleep(300)  # Espera inicial 5 minutos
    process = psutil.Process(os.getpid())
    print("🛡️ RAM and connection monitor started.", flush=True)
    
    consecutive_high_mem = 0
    consecutive_high_latency = 0
    check_interval = 600  # 10 min entre chequeos
    while True:
        try:
            mem_mb = process.memory_info().rss / (1024 * 1024)
            latency_ms = bot.latency * 1000
            print(f"📦 Memory: {mem_mb:.2f} MB | 🌐 Latency: {latency_ms:.0f} ms", flush=True)
            # Chequeo gradual para evitar reinicio por pico temporal
            if mem_mb >= 490:
                consecutive_high_mem += 1
            else:
                consecutive_high_mem = 0
            if latency_ms > 1000:
                consecutive_high_latency += 1
            else:
                consecutive_high_latency = 0
            if consecutive_high_mem >= 2 or consecutive_high_latency >= 2 or bot.is_closed() or not bot.is_ready():
                with monitor_lock:  # Solo un thread reiniciando
                    print(f"⚠️ Restart triggered. Memory: {mem_mb:.2f}, Latency: {latency_ms:.0f} ms", flush=True)
                    sys.exit(1)
            print("✅ Bot check passed.", flush=True)
            time.sleep(check_interval)
        except Exception as e:
            print(f"❌ Error in monitor_bot: {e}", flush=True)
            time.sleep(10)

# ───────────── Error Handler ─────────────
logging.basicConfig(
    filename="bot_errors.log",
    level=logging.ERROR,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    async def safe_send(msg: str):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass
    if isinstance(error, app_commands.MissingPermissions):
        await safe_send("❌ You lack permission to do that.")
    elif isinstance(error, app_commands.CommandOnCooldown):
        await safe_send("⏳ This command is on cooldown. Try again later.")
    elif isinstance(error, app_commands.CheckFailure):
        await safe_send("❌ You don't meet the command requirements.")
    else:
        logging.error("Unexpected error:\n%s", traceback.format_exc())
        await safe_send("⚠️ An unexpected error occurred.")

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
    sys.exit(1)


