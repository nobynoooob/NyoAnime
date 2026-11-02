<div align="center">

  <h1>NyoAnime</h1>

  <p>
    <strong>مشغّل ومُنزّل أنمي مباشر من الطرفية</strong><br>
    شاهد ونزّل الأنمي بجودة عالية مع ترجمات عربية وإنجليزية مباشرة من الطرفية.
  </p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+" />
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL--3.0-22c55e?style=flat-square" alt="License" /></a>
  </p>

  <p>
    <strong><a href="README.md">Read in English</a></strong>
  </p>

  <hr />

  <p>
    <a href="#المزوّدون">المزوّدون</a> &nbsp;•&nbsp;
    <a href="#التثبيت">التثبيت</a> &nbsp;•&nbsp;
    <a href="#الاستخدام">الاستخدام</a> &nbsp;•&nbsp;
    <a href="#اختصارات-لوحة-المفاتيح">الاختصارات</a> &nbsp;•&nbsp;
    <a href="#الإعدادات">الإعدادات</a> &nbsp;•&nbsp;
    <a href="#الترخيص">الترخيص</a>
  </p>

</div>

<br />

<div align="center">
  <img src="assets/showcase.gif" alt="NyoAnime" />
</div>

---

## المميزات

- **محرّك متعدد المزوّدين** — Anime3rb وAnime Slayer وAnimeify وHiAnime وStardima وStardima Kids.
- **مُنزّل مدمج** — تنزيل متوازٍ مع إيقاف واستئناف وسرعة ووقت متبقٍ مباشر، دون الحاجة إلى `aria2c` أو `yt-dlp`.
- **وضع تفاعلي خفيف (`-i`)** — واجهة بحث عبر `fzf` مع دعم سليم للنص العربي من اليمين إلى اليسار.
- **دعم عدة مشغّلات** — MPV وVLC وCelluloid وIINA وSyncplay، مع حفظ دقيق لموضع التوقف عبر MPV IPC.
- **بحث عربي ذكي** — تطبيع للهمزة والتاء المربوطة والأسماء المكتوبة باللاتينية.
- **إعدادات وسجل محلي بالكامل** — بلا تحليلات أو تتبّع أو بثّ إعلانات أو فحص تحديثات.

---

## المزوّدون

يمكنك تبديل المزوّد من الإعدادات (`c` / `O`) أو عبر الخيار `-p`:

| المزوّد | مفتاح CLI | اللغة | ملاحظات |
| :--- | :---: | :---: | :--- |
| **Anime3rb** | `anime3rb` | عربي مترجم | روابط CDN مباشرة مع اختيار الجودة |
| **Anime Slayer** | `anime_slayer` | عربي مترجم | واجهات API رسمية مع سيرفرات بديلة |
| **Animeify** | `animeify` | عربي مترجم | سيرفرات متعددة وتبديل سريع للجودة |
| **HiAnime Sub** | `hianime` | إنجليزي مترجم | جودات HLS ومسار ترجمة إنجليزية مع Referer صحيح |
| **HiAnime Dub** | `hianime_dub` | إنجليزي مدبلج | مصدر صوت إنجليزي بنفس محرك الاستخراج |
| **Stardima** | `stardima` | عربي مدبلج | بث متعدد السيرفرات مع اختيار الصوت العربي |
| **Stardima Kids** | `stardima_kids` | عربي مدبلج | كرتون ومسلسلات أطفال مدبلجة |

---

## التثبيت

### المتطلبات

- **Python 3.9+** (يُفضّل `3.10`–`3.12`)
- **MPV** — المشغّل الموصى به مع استئناف الموضع
- **FFmpeg** — لمعالجة البث
- **fzf** — مطلوب للوضع التفاعلي `-i`

### من المصدر

```bash
git clone <repository-url> nyoanime
cd nyoanime
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

ثم شغّل:

```bash
nyoanime
# أو الاختصار
nyo
```

يمكنك أيضاً التشغيل دون تثبيت:

```bash
python3 main.py
```

---

## الاستخدام

### واجهة الطرفية الكاملة (الافتراضي)

```bash
nyo
```

### البحث التفاعلي الخفيف

```bash
nyo -i "One Piece"
nyo -i "ون بيس"
```

### اختيار مزوّد

```bash
nyo -p anime3rb "Naruto"
nyo -p hianime "Attack on Titan"
nyo -p stardima "ناروتو"
nyo -p stardima_kids "سبونج بوب"
```

### بحث مباشر في الواجهة

```bash
nyo "Hunter x Hunter"
```

### خيارات أخرى

```bash
nyo -d              # سجل تصحيح مفصّل
nyo -v              # إظهار الإصدار
nyo --help          # دليل الخيارات
```

---

## اختصارات لوحة المفاتيح

### الواجهة الرئيسية

| المفتاح | الإجراء |
| :---: | :--- |
| <kbd>↑</kbd> <kbd>↓</kbd> / <kbd>j</kbd> <kbd>k</kbd> | التنقل بين القوائم |
| <kbd>Enter</kbd> | بحث / تأكيد |
| <kbd>e</kbd> / <kbd>m</kbd> | أحدث الحلقات / الأفلام |
| <kbd>t</kbd> / <kbd>p</kbd> / <kbd>r</kbd> | الرائج / الشائع / الأعلى تقييماً |
| <kbd>g</kbd> / <kbd>s</kbd> | التصنيفات / الاستوديوهات |
| <kbd>l</kbd> / <kbd>f</kbd> | السجل / المفضلة |
| <kbd>c</kbd> / <kbd>O</kbd> | الإعدادات |
| <kbd>B</kbd> | رجوع |
| <kbd>Ctrl+C</kbd> / <kbd>Q</kbd> | خروج |

### MPV

| المفتاح | الإجراء |
| :---: | :--- |
| <kbd>Space</kbd> | إيقاف / متابعة |
| <kbd>←</kbd> / <kbd>→</kbd> | تقديم/تأخير 5 ثوانٍ |
| <kbd>[</kbd> / <kbd>]</kbd> | سرعة التشغيل |
| <kbd>F</kbd> | ملء الشاشة |
| <kbd>9</kbd> / <kbd>0</kbd> | مستوى الصوت |
| <kbd>Q</kbd> | إيقاف وحفظ موضع المتابعة |

---

## الإعدادات

تُحفظ التفضيلات في:

```text
~/.nyoanime/database/config.json
```

افتح الإعدادات عبر <kbd>c</kbd> أو <kbd>O</kbd> لتغيير:

- اللغة المفضلة (`ar_sub` أو `en_sub` أو `en_dub`)
- الاختيار التلقائي للمزوّد
- المزوّد النشط
- لغة صوت Stardima (`dub` أو `sub` أو `both`)
- جودة البث والتنزيل الافتراضية
- المشغّل
- الانتقال التلقائي للحلقة التالية
- Discord Rich Presence (معطّل افتراضياً)
- سمة الألوان
- مجلد التنزيل
- سجل التصحيح

لا يتصل Discord Rich Presence إلا عند ضبط `NYOANIME_DISCORD_CLIENT_ID` في البيئة. لا تُرسل أي بيانات خارج جهازك عدا طلبات المزوّدين الذين تستخدمهم صراحةً.

---

## التطوير

```bash
pip install -r requirements.txt
python3 -m pytest -q
```

لبناء ملف تنفيذي مستقل استخدم `scripts/build.py` (يتطلب PyInstaller).

---

## الترخيص

رخصة GNU General Public License v3.0. راجع [LICENSE](LICENSE) و[NOTICE](NOTICE) لإسناد ani-cli الخاص بمزوّد HiAnime.

<div align="center">
  <p>صُنع وصيانة بواسطة <strong>NoBy</strong>.</p>
</div>
