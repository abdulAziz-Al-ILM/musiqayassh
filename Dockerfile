# Pythonning eng so'nggi stabil va yengil versiyasini tanlaymiz
FROM python:3.10-slim

# Tizim yangilanadi va audio ishlash uchun kerakli bo'lgan FFmpeg o'rnatiladi
# FFmpeg pydub kutubxonasi uchun juda muhim!
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Ishchi papkani belgilaymiz
WORKDIR /app

# 1. Kutubxonalarni o'rnatamiz
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2. Barcha kodlarni va 'samples' papkasini ko'chiramiz
COPY . .

# 3. Vaqtinchalik fayllar uchun papkalar yaratamiz (agar ular bo'lmasa)
RUN mkdir -p downloads samples

# Botni ishga tushirish buyrug'i
CMD ["python", "main.py"]
