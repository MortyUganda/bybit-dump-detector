import asyncio, os
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

async def main():
    b = Bot(token=os.environ['TELEGRAM_BOT_TOKEN'], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    text = (
        '<b>Test signal canceled</b>\n\n'
        '<a href="https://www.bybit.com/trade/usdt/LITUSDT">LITUSDT</a>\n'
        'Score: <b>45</b>\n'
        'Reason: trend_filter_blocked\n'
        '<i>test message</i>'
    )
    try:
        r = await b.send_message(chat_id=1011756765, text=text, parse_mode='HTML')
        print('OK msg_id:', r.message_id)
    except Exception as e:
        print('ERROR:', type(e).__name__, e)
    finally:
        await b.session.close()

asyncio.run(main())
