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

    doc = points_collection.find_one({"guild_id": interaction.guild.id, "user_id": member.id})

    if not doc or (doc.get("tp", 0) == 0 and doc.get("mp", 0) == 0):
        msg = await interaction.followup.send(f"_**{member.display_name}** does not have any points to show their profile yet._")
        await asyncio.sleep(15)
        with contextlib.suppress((discord.Forbidden, discord.NotFound)):
            await msg.delete()
        return

    highest_rank_raw = get_highest_rank(member)
    current_rank = highest_rank_raw.split("|")[-1].strip() if "|" in highest_rank_raw else highest_rank_raw

    embed = discord.Embed(title=f"{member.display_name}", color=discord.Color.from_rgb(247, 240, 172))
    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(name="**Training Points**", value=doc.get("tp", 0), inline=True)
    embed.add_field(name="**Mission Points**", value=doc.get("mp", 0), inline=True)

    embed.add_field(
        name="",
        value="<:H1Laser:1395749428135985333><:H2Laser:1395749449753563209><:R1Laser:1395746456681578628><:R1Laser:1395746456681578628><:R1Laser:1395746456681578628><:R1Laser:1395746456681578628><:R2Laser:1395746474293198949> " \
          "<:M2LaserInv:1395909504482283750><:M1Laser:1395909456986112110><:M1Laser:1395909456986112110><:M1Laser:1395909456986112110><:M1Laser:1395909456986112110><:H2LaserInv:1395909361494396948><:H1LaserInv:1395909332339790065>",
        inline=False
    )

    # Medallas
    user_medals = [emoji for role_id, emoji in medal_roles.items() if discord.utils.get(member.roles, id=role_id)]
    if user_medals:
        embed.add_field(name="**Medals of honor**", value=" {} ".format(" | ".join(user_medals)), inline=False)

    embed.add_field(name="**Rank**", value=f"{rank_emojis.get(current_rank, '')} | {current_rank}", inline=False)

    # Level-Tier
    member_role_ids = [role.id for role in member.roles]
    level_tier = None

    if tier_roles["✩ Legend-Tier"] in member_role_ids:
        level_tier = "✩ Legend-Tier"
    elif tier_roles["★ Ashenlight-Tier"] in member_role_ids:
        level_tier = "★ Ashenlight-Tier"
    elif tier_roles["Celestial-Tier"] in member_role_ids:
        level_tier = "Celestial-Tier"
    elif tier_roles["Elite-Tier"] in member_role_ids:
        level_tier = "Elite-Tier"
    else:
        for base in ["Celestial-Tier", "Elite-Tier", "High-Tier", "Middle-Tier", "Low-Tier"]:
            if tier_roles[base] in member_role_ids:
                if tier_roles["[ ⁂ ]"] in member_role_ids:
                    level_tier = f"{base} [ ⁂ ]"
                elif tier_roles["[ ⁑ ]"] in member_role_ids:
                    level_tier = f"{base} [ ⁑ ]"
                else:
                    level_tier = base
                break

    if level_tier:
        base_tier = level_tier.split(" [")[0]  # Extrae solo el nombre base del Tier
        emoji = tier_emojis.get(base_tier, "")
        embed.add_field(name="**Level-Tier**", value=f"{emoji} {level_tier}", inline=False)

    # Requisitos o texto especial
    next_rank = None
    if current_rank in rank_list:
        current_index = rank_list.index(current_rank)
        if current_index + 1 < len(rank_list):
            next_rank = rank_list[current_index + 1]

    if current_rank in ["Gray Lord", "Ashen Lord"]:
        embed.add_field(name="", value="You are part of the TGO council.", inline=False)
    elif current_rank in ["Silver Knight", "Master - On trial", "Grandmaster", "Master of Balance"]:
        if next_rank:
            embed.add_field(
                name="**Next rank**",
                value=f"{rank_emojis.get(next_rank, '')} | {next_rank}\nFrom this rank onwards, promotions are decided by HR.",
                inline=False
            )
    elif next_rank in rank_requirements:
        req = rank_requirements[next_rank]
        req_text = (
            f"**Next rank**\n{rank_emojis.get(next_rank, '')} | {next_rank}\n"
            f"\u00b7 _**{req.get('tp', 0)}** training points_\n"
            f"\u00b7 _**{req.get('mp', 0)}** mission points_\n"
        )
        if req.get("tier"):
            req_text += f"\u00b7 _**{req['tier']}** level_"
        embed.add_field(name="", value=req_text, inline=False)

    embed.add_field(name="\u200b", value="-# <:OficialTGO:1395904116072648764> The Gray Order", inline=False)
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
       
# ───────────── /addtier ─────────────
@bot.tree.command(name="addtier", description="Set or update a member's tier level")
@app_commands.describe(
    member="Member to assign tier",
    level="Tier role to assign (mention the role)",
    stars="Stars level, 2 or 3 (optional)",
)
async def addtier(
    interaction: discord.Interaction,
    member: discord.Member,
    level: discord.Role,
    stars: app_commands.Range[int, 2, 3] | None = None,
):
    caller = cast(discord.Member, interaction.user)
    if not has_permission(caller):
        await interaction.response.send_message("❌ You lack permission.", ephemeral=True)
        return

    await interaction.response.defer()

    level_name = level.name
    tier_role_id = tier_roles.get(level_name)

    if not tier_role_id:
        msg = await interaction.followup.send("❌ Invalid tier level. Make sure you provide a valid Tier.")
        await asyncio.sleep(15)
        with contextlib.suppress((discord.Forbidden, discord.NotFound)):
            await msg.delete()
        return

    # Estrellas válidas solo para estos niveles
    valid_star_levels = {"Low-Tier", "Middle-Tier", "High-Tier", "Elite-Tier", "Celestial-Tier"}
    if stars and level_name not in valid_star_levels:
        msg = await interaction.followup.send("❌ Only Low-Tier, Middle-Tier, High-Tier, Elite-Tier and Celestial-Tier can receive stars.")
        await asyncio.sleep(15)
        with contextlib.suppress((discord.Forbidden, discord.NotFound)):
            await msg.delete()
        return

    # Eliminar cualquier Tier anterior (incluyendo estrellas previas)
    roles_to_remove = []
    for rid in tier_roles.values():
        role_obj = discord.utils.get(interaction.guild.roles, id=rid)
        if role_obj and role_obj in member.roles:
            roles_to_remove.append(role_obj)

    if roles_to_remove:
        await member.remove_roles(*roles_to_remove)

    # Añadir el nuevo rol de Tier
    new_tier_role = discord.utils.get(interaction.guild.roles, id=tier_role_id)
    added_roles = []

    if new_tier_role:
        await member.add_roles(new_tier_role)
        added_roles.append(new_tier_role.mention)

    # Añadir estrella si es válida
    if stars:
        star_label = "[ ⁑ ]" if stars == 2 else "[ ⁂ ]"
        star_role_id = tier_roles.get(star_label)
        star_role = discord.utils.get(interaction.guild.roles, id=star_role_id)
        if star_role:
            await member.add_roles(star_role)
            added_roles.append(star_role.mention)

    # Confirmación visual
    embed = discord.Embed(
        title="🎇 Tier Updated",
        description=f"{member.mention} has been assigned the Tier: **{level_name}**" +
                    (f"\nStars: **{stars}**" if stars else "") +
                    f"\nRoles given: {' | '.join(added_roles)}",
        color=discord.Color.from_rgb(141, 228, 212)
    )
    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(15)
    with contextlib.suppress((discord.Forbidden, discord.NotFound)):
        await msg.delete()


# ───────────── /tierlist ─────────────
@bot.tree.command(name="tierlist", description="Show top 25 members sorted by Tier and group rank")
async def tierlist(interaction: discord.Interaction):
    await interaction.response.defer()

    guild = interaction.guild
    members = guild.members

    # Orden de los tiers de mayor a menor (mejor primero)
    tier_order = [
        "✩ Legend-Tier",
        "★ Ashenlight-Tier",
        "Celestial-Tier",
        "Elite-Tier",
        "High-Tier",
        "Middle-Tier",
        "Low-Tier"
    ]

    def get_member_tier(member: discord.Member) -> str | None:
        """
        Obtiene el tier completo del miembro (incluyendo estrellas si las tiene).
        Devuelve None si no tiene Tier.
        """
        role_ids = {role.id for role in member.roles}
        base_tier = None
        for tier in tier_order:
            if tier_roles.get(tier) in role_ids:
                base_tier = tier
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
        """
        Obtiene el rango principal del miembro según group_ranks_order.
        """
        member_role_names = {role.name for role in member.roles}
        for rank in group_ranks_order:
            if rank in member_role_names:
                return rank
        return None

    members_with_tier = []
    for member in members:
        tier = get_member_tier(member)
        if tier:
            rank = get_member_rank(member) or "Initiate"
            members_with_tier.append((member, tier, rank))

    if not members_with_tier:
        await interaction.followup.send("No members with Tier roles found.")
        return

    def parse_tier_components(tier_name: str):
        """
        Devuelve una tupla con: (índice base, estrellas) para ordenar correctamente.
        """
        stars = 0
        if "[ ⁂ ]" in tier_name:
            stars = 3
        elif "[ ⁑ ]" in tier_name:
            stars = 2

        base = tier_name.split(" [")[0].strip()
        base_index = tier_order.index(base) if base in tier_order else len(tier_order)
        return (base_index, -stars)  # Más estrellas = mejor

    def rank_index(rank_name: str) -> int:
        return group_ranks_order.index(rank_name) if rank_name in group_ranks_order else len(group_ranks_order)

    # Ordenar: primero Tier base, luego estrellas (más estrellas mejor), luego rango (más alto mejor)
    members_with_tier.sort(key=lambda x: (
        parse_tier_components(x[1]),
        rank_index(x[2])
    ))

    top_members = members_with_tier[:25]

    lines = []
    for i, (member, tier, _) in enumerate(top_members, start=1):
        lines.append(f"{i}. {member.display_name} — {tier}")

    invoker_pos = None
    invoker_id = interaction.user.id
    for i, (member, _, _) in enumerate(members_with_tier, start=1):
        if member.id == invoker_id:
            invoker_pos = i
            break

    footer_text = f"Your position is: {invoker_pos}" if invoker_pos else "You have no Tier position."

    embed = discord.Embed(
        title="🏆 Tier Leaderboard",
        description="\n".join(lines),
        color=discord.Color.from_rgb(255, 255, 255)  # Blanco puro
    )
    embed.set_footer(text=footer_text)

    msg = await interaction.followup.send(embed=embed)
    await asyncio.sleep(60)
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
