import logging
import os
import sys
import asyncio
import math
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import LabeledPrice, PreCheckoutQuery, ContentType, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import aiosqlite
from pydub import AudioSegment

# --- SOZLAMALAR ---
# Railway o'zgaruvchilaridan olinadi. Agar mahalliy kompyuterda bo'lsa ikkinchi qiymat ishlaydi.
BOT_TOKEN = os.getenv("BOT_TOKEN", "SIZNING_BOT_TOKENINGIZ")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN", "CLICK_YOKI_PAYME_TOKEN") 
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

DB_NAME = "music_bot.db"

# --- LIMITLAR VA NARXLAR ---
LIMITS = {
    "free": {"duration": 20, "daily": 8, "instruments": 8},
    "plus": {"duration": 120, "daily": 24, "instruments": 12},
    "pro": {"duration": 600, "daily": 50, "instruments": 20}
}

PRICE_PLUS = 24000 * 100 # Tiyinda
PRICE_PRO = 50000 * 100

# Asboblar ro'yxati (setup_samples.py dagi nomlar bilan bir xil bo'lishi shart)
INSTRUMENTS_LIST = [
    # Free (8 ta)
    "Piano", "Guitar", "Drum", "Violin", "Flute", "Bass", "Saxophone", "Trumpet",
    # Plus (+4 ta)
    "Cello", "Harp", "Clarinet", "Oboe",
    # Pro (+8 ta)
    "PhonkCowbell", "PhonkBass", "Synth", "808", "ElectricGuitar", "Koto", "Sitar", "Banjo"
]

# --- AUDIO ENGINE (Musiqa Dvigateli) ---
class AudioEngine:
    def __init__(self):
        self.base_path = "samples"
        # Agar papka bo'lmasa yaratib qo'yadi
        if not os.path.exists(self.base_path):
            os.makedirs(self.base_path)

    def change_pitch(self, segment, semitones):
        """Pydub yordamida pitch (ton) o'zgartirish"""
        new_sample_rate = int(segment.frame_rate * (2.0 ** (semitones / 12.0)))
        return segment._spawn(segment.raw_data, overrides={'frame_rate': new_sample_rate}).set_frame_rate(44100)

    def process(self, input_path, instrument_name, output_path):
        try:
            original_audio = AudioSegment.from_file(input_path)
        except Exception as e:
            logging.error(f"Audio o'qishda xatolik: {e}")
            return False

        # 1. Asbob faylini topish
        sample_path = os.path.join(self.base_path, f"{instrument_name}.wav")
        
        # Agar so'ralgan asbob bo'lmasa, papkadagi birinchi faylni oladi (Fallback)
        if not os.path.exists(sample_path):
            files = [f for f in os.listdir(self.base_path) if f.endswith('.wav')]
            if files:
                sample_path = os.path.join(self.base_path, files[0])
            else:
                # Agar umuman sample bo'lmasa, audio o'zgarishsiz qaytadi
                original_audio.export(output_path, format="mp3")
                return True

        # 2. Asbobni yuklash
        base_sample = AudioSegment.from_file(sample_path)
        
        # 3. Qayta ishlash parametrlari
        chunk_ms = 150 # 150 millisekundlik bo'laklar
        generated = AudioSegment.silent(duration=len(original_audio))
        
        # Audioni bo'laklarga bo'lish
        chunks = [original_audio[i:i+chunk_ms] for i in range(0, len(original_audio), chunk_ms)]

        for i in range(len(chunks) - 1):
            curr = chunks[i]
            curr_vol = curr.rms # Ovoz balandligi
            
            if curr_vol < 50: continue # Sukunatni o'tkazib yuboramiz

            # MANTIQ: Ovoz balandligiga qarab tonni o'zgartirish
            # Bu usul bitta fayldan turli notalar yasaydi
            note = base_sample
            
            if curr_vol > 3000:
                note = self.change_pitch(base_sample, 12) # +1 Oktava
            elif curr_vol > 1500:
                note = self.change_pitch(base_sample, 7)  # +7 yarim ton
            elif curr_vol > 800:
                note = self.change_pitch(base_sample, 4)  # +4 yarim ton
            elif curr_vol > 400:
                note = base_sample # Original
            else:
                note = self.change_pitch(base_sample, -5) # -5 yarim ton

            # Semplni vaqt oynasiga sig'dirish va yumshatish
            valid_len = min(len(note), chunk_ms * 2) # Biroz uzunroq qolishi mumkin (reverb effect uchun)
            note = note[:valid_len].fade_out(30)
            
            position = i * chunk_ms
            generated = generated.overlay(note, position=position)

        # 4. Eksport
        # Bitrateni pasaytiramiz (bot tez ishlashi uchun)
        generated.export(output_path, format="mp3", bitrate="128k")
        return True

audio_engine = AudioEngine()

# --- MA'LUMOTLAR BAZASI (Async) ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE,
                username TEXT,
                status TEXT DEFAULT 'free',
                sub_end_date TEXT,
                daily_usage INTEGER DEFAULT 0,
                last_usage_date TEXT,
                referrer_id INTEGER,
                join_date TEXT,
                bonus_limit INTEGER DEFAULT 0
            )
        """)
        await db.commit()

async def get_user(telegram_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
            return await cursor.fetchone()

async def register_user(telegram_id, username, referrer_id=None):
    today = datetime.now().date().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("""
                INSERT INTO users (telegram_id, username, referrer_id, join_date, last_usage_date)
                VALUES (?, ?, ?, ?, ?)
            """, (telegram_id, username, referrer_id, datetime.now().isoformat(), today))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def check_status_and_limits(telegram_id):
    """Foydalanuvchi holatini, obunasini va limitlarini tekshirib yangilaydi"""
    today = datetime.now().date().isoformat()
    user = await get_user(telegram_id)
    
    if not user: return None
    
    updated = False
    # 1. Kunlik limit reset
    if user[6] != today:
        async with aiosqlite.connect(DB_NAME) as db:
            # Kun o'zgarganda bonuslar ham 0 ga tushadi (shart bo'yicha: "o'sha kun uchun")
            await db.execute("UPDATE users SET daily_usage = 0, bonus_limit = 0, last_usage_date = ? WHERE telegram_id = ?", (today, telegram_id))
            await db.commit()
        updated = True

    # 2. Obuna muddati tekshiruvi
    if user[3] in ['plus', 'pro'] and user[4]:
        end_date = datetime.fromisoformat(user[4])
        if datetime.now() > end_date:
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET status = 'free', sub_end_date = NULL WHERE telegram_id = ?", (telegram_id,))
                await db.commit()
            updated = True
    
    if updated:
        return await get_user(telegram_id)
    return user

# --- REFERAL TIZIMI ---
async def trigger_referral_bonus(user_id, action_type):
    """
    action_type: 
    'usage' -> +2 limit
    'plus'  -> +8 limit
    'pro'   -> +16 limit
    """
    user = await get_user(user_id)
    if not user or not user[7]: return # Referrer yo'q

    referrer_id = user[7]
    join_date = datetime.fromisoformat(user[8])

    # Agar ro'yxatdan o'tganiga 7 kundan oshmagan bo'lsa
    if datetime.now() < join_date + timedelta(days=7):
        bonus = 0
        if action_type == 'usage': bonus = 2
        elif action_type == 'plus': bonus = 8
        elif action_type == 'pro': bonus = 16
        
        if bonus > 0:
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET bonus_limit = bonus_limit + ? WHERE telegram_id = ?", (bonus, referrer_id))
                await db.commit()
            
            try:
                await bot.send_message(referrer_id, f"🎉 Referal bonusi! Siz chaqirgan do'stingiz faolligi sababli bugungi limitingizga +{bonus} ta qo'shildi.")
            except: pass

# --- BOT SETUP ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- KEYBOARDS ---
def main_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🎹 Musiqa yasash")
    kb.button(text="🌟 Plus Obuna")
    kb.button(text="🚀 Pro Obuna")
    kb.button(text="📊 Statistika")
    kb.button(text="ℹ️ Yordam")
    kb.button(text="📢 Reklama")
    kb.adjust(2, 2, 2)
    return kb.as_markup(resize_keyboard=True)

def instruments_kb(status):
    kb = InlineKeyboardBuilder()
    limit = LIMITS[status]['instruments']
    # Foydalanuvchi statusiga mos asboblarni ko'rsatish
    available = INSTRUMENTS_LIST[:limit]
    
    for inst in available:
        kb.button(text=inst, callback_data=f"instr_{inst}")
    kb.adjust(3) # Bir qatorda 3 tadan
    return kb.as_markup()

# --- HANDLERS: ASOSIY ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    ref_id = None
    if command.args and command.args.isdigit():
        if int(command.args) != message.from_user.id:
            ref_id = int(command.args)
    
    is_new = await register_user(message.from_user.id, message.from_user.username, ref_id)
    
    text = (
        f"Assalomu alaykum, {message.from_user.full_name}!\n\n"
        "🎙 Menga ovozli xabar yoki musiqa yuboring, men uni musiqa asboblarida chalib beraman.\n\n"
        "Bepul versiya: Kuniga 8 marta.\n"
        "Ko'proq imkoniyat uchun tugmalardan foydalaning 👇"
    )
    await message.answer(text, reply_markup=main_kb())

@dp.message(F.text == "📊 Statistika")
async def stats_view(message: types.Message):
    user = await check_status_and_limits(message.from_user.id)
    
    status = user[3]
    base_limit = LIMITS[status]['daily']
    bonus_limit = user[9]
    used = user[5]
    total_limit = base_limit + bonus_limit
    
    link = f"https://t.me/{(await bot.get_me()).username}?start={message.from_user.id}"
    
    text = (
        f"👤 **Sizning Kabinetingiz**\n\n"
        f"🔰 Status: **{status.upper()}**\n"
        f"⏳ Obuna tugash vaqti: {user[4] if user[4] else 'Cheksiz'}\n"
        f"🎚 Bugungi limit: {used} / {total_limit} (Bonuslar: {bonus_limit})\n\n"
        f"🔗 **Referal havolangiz:**\n`{link}`\n\n"
        f"Do'stingizni taklif qiling:\n"
        f"• U ishlatsa: +2 limit\n"
        f"• Plus olsa: +8 limit\n"
        f"• Pro olsa: +16 limit (faqat bugun uchun)"
    )
    await message.answer(text, parse_mode="Markdown")

# --- HANDLERS: TO'LOV ---
@dp.message(F.text.in_({"🌟 Plus Obuna", "🚀 Pro Obuna"}))
async def subscribe_handler(message: types.Message):
    if "Plus" in message.text:
        amount = PRICE_PLUS
        title = "Plus Obuna (31 kun)"
        desc = "12 ta asbob, 2 daqiqa audio, kuniga 24 limit"
        payload = "sub_plus"
    else:
        amount = PRICE_PRO
        title = "Pro Obuna (31 kun)"
        desc = "20 ta asbob, Phonk ovozlar, 10 daqiqa audio, 50 limit"
        payload = "sub_pro"

    await bot.send_invoice(
        chat_id=message.chat.id,
        title=title,
        description=desc,
        payload=payload,
        provider_token=PAYMENT_TOKEN,
        currency="UZS",
        prices=[LabeledPrice(label="Obuna narxi", amount=amount)],
        start_parameter="create_sub"
    )

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)

@dp.message(F.successful_payment)
async def success_pay(message: types.Message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    user_id = message.from_user.id
    
    new_status = "plus" if "plus" in payload else "pro"
    end_date = (datetime.now() + timedelta(days=31)).isoformat()
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET status = ?, sub_end_date = ? WHERE telegram_id = ?", (new_status, end_date, user_id))
        await db.commit()
    
    await trigger_referral_bonus(user_id, new_status)
    await message.answer(f"✅ To'lov muvaffaqiyatli! Siz {new_status.upper()} a'zosisiz.")

# --- HANDLERS: AUDIO PROCESS ---
class AudioState(StatesGroup):
    waiting_audio = State()
    waiting_instrument = State()

@dp.message(F.text == "🎹 Musiqa yasash")
async def start_process(message: types.Message, state: FSMContext):
    await message.answer("Menga audio fayl yoki ovozli xabar (Golosovoy) yuboring.")
    await state.set_state(AudioState.waiting_audio)

@dp.message(AudioState.waiting_audio, F.content_type.in_([ContentType.AUDIO, ContentType.VOICE]))
async def receive_audio(message: types.Message, state: FSMContext):
    user = await check_status_and_limits(message.from_user.id)
    status = user[3]
    
    # 1. Limit tekshirish
    limit_total = LIMITS[status]['daily'] + user[9]
    if user[5] >= limit_total:
        await message.answer("❌ Bugungi limitingiz tugadi. Ertaga kuting yoki obuna bo'ling.")
        await state.clear()
        return

    # 2. Faylni yuklash
    file_id = message.voice.file_id if message.voice else message.audio.file_id
    
    # Faylni vaqtincha saqlash
    os.makedirs("downloads", exist_ok=True)
    file_path = f"downloads/{file_id}.ogg"
    
    file_info = await bot.get_file(file_id)
    await bot.download_file(file_info.file_path, file_path)
    
    # 3. Uzunlikni tekshirish
    try:
        audio = AudioSegment.from_file(file_path)
        duration = len(audio) / 1000
        max_dur = LIMITS[status]['duration']
        
        if duration > max_dur:
            await message.answer(f"❌ Audio juda uzun ({int(duration)}s). Sizning limit: {max_dur}s")
            os.remove(file_path)
            await state.clear()
            return
    except:
        await message.answer("❌ Faylni o'qib bo'lmadi.")
        os.remove(file_path)
        await state.clear()
        return

    await state.update_data(file_path=file_path)
    await message.answer("Qaysi musiqa asbobida chalamiz?", reply_markup=instruments_kb(status))
    await state.set_state(AudioState.waiting_instrument)

@dp.callback_query(AudioState.waiting_instrument, F.data.startswith("instr_"))
async def convert_audio(call: types.CallbackQuery, state: FSMContext):
    instrument = call.data.split("_")[1]
    data = await state.get_data()
    input_path = data['file_path']
    output_path = input_path.replace(".ogg", ".mp3")
    
    await call.message.edit_text(f"⏳ {instrument} asbobiga o'girilmoqda...\nBiroz kuting.")
    
    # --- ASOSIY ISH ---
    # Bloklamaslik uchun alohida thread'da bajaramiz
    success = await asyncio.to_thread(audio_engine.process, input_path, instrument, output_path)
    
    if success:
        result = FSInputFile(output_path)
        await bot.send_audio(
            call.from_user.id, 
            result, 
            caption=f"🎹 Asbob: {instrument}\n👤 Status: {(await get_user(call.from_user.id))[3].upper()}"
        )
        
        # Limitni yangilash
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET daily_usage = daily_usage + 1 WHERE telegram_id = ?", (call.from_user.id,))
            await db.commit()
            
        # Referal (usage) bonusi berish
        await trigger_referral_bonus(call.from_user.id, 'usage')
        
        # Tozalash
        try: os.remove(output_path)
        except: pass
    else:
        await call.message.answer("❌ Qandaydir xatolik yuz berdi. Boshqa audio bilan urinib ko'ring.")

    try: os.remove(input_path)
    except: pass
    await state.clear()

# --- HANDLERS: QO'SHIMCHA ---
@dp.message(F.text == "📢 Reklama")
async def ads_handler(message: types.Message):
    await message.answer(f"Reklama bo'yicha adminga murojaat qiling: @Admin (ID: {ADMIN_ID})")

@dp.message(F.text == "ℹ️ Yordam")
async def help_handler(message: types.Message):
    await message.answer("Botdan foydalanish oson:\n1. 'Musiqa yasash' ni bosing.\n2. Audio yuboring.\n3. Asbob tanlang.\n\nReferal havola orqali do'stlaringizni chaqirsangiz, ko'proq imkoniyat olasiz!")

@dp.message(F.text == "📄 ToU")
async def tou_handler(message: types.Message):
    await message.answer("Foydalanish qoidalari:\n1. Botdan noqonuniy maqsadlarda foydalanish taqiqlanadi.\n2. Admin to'lovlarni qaytarmaslik huquqiga ega.")

# --- ADMIN (Minimal) ---
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_stats(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            count = (await cursor.fetchone())[0]
    await message.answer(f"Admin Panel\nJami foydalanuvchilar: {count}")

@dp.message(Command("broadcast"), F.from_user.id == ADMIN_ID)
async def broadcast(message: types.Message, command: CommandObject):
    if not command.args:
        await message.answer("Xabar yozing. Masalan: /broadcast Salom")
        return
    
    msg = command.args
    count = 0
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT telegram_id FROM users") as cursor:
            async for row in cursor:
                try:
                    await bot.send_message(row[0], msg)
                    count += 1
                    await asyncio.sleep(0.05)
                except: pass
    await message.answer(f"Xabar {count} kishiga bordi.")

# --- MAIN FUNCTION ---
async def main():
    await init_db()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    print("Bot ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot to'xtatildi")
