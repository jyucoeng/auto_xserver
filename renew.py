#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XServer GAME 自动登录和续期脚本
"""

# =====================================================================
#                          导入依赖
# =====================================================================

import asyncio
import time
import re
import datetime
from datetime import timezone, timedelta
import os
import json
import requests
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page
from playwright_stealth import stealth_async

# =====================================================================
#                          配置区域
# =====================================================================

# 浏览器配置
IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
USE_HEADLESS = IS_GITHUB_ACTIONS or os.getenv("USE_HEADLESS", "false").lower() == "true"
WAIT_TIMEOUT = 10000     # 页面元素等待超时时间(毫秒)
PAGE_LOAD_DELAY = 3      # 页面加载延迟时间(秒)

# 代理配置 - 可选，不填则不使用代理
PROXY_SERVER = os.getenv("PROXY_SERVER") or "填你socks5"
USE_PROXY = bool(PROXY_SERVER)  # 如果有代理地址则启用

# XServer登录配置 - 可以直接填写或使用环境变量
LOGIN_EMAIL = os.getenv("XSERVER_EMAIL") or "填你邮箱"
LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD") or "填你密码"
TARGET_URL = "https://secure.xserver.ne.jp/xapanel/login/xmgame"

# Telegram配置 - 可选，不填则不推送
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "填你TG机器人token"
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or "填你TG ID"

# =====================================================================
#                        Telegram 推送模块
# =====================================================================

class TelegramNotifier:
    """Telegram 通知推送类"""
    
    def __init__(self, bot_token=None, chat_id=None):
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)
        
        if not self.enabled:
            print("ℹ️ Telegram 推送未启用(缺少 BOT_TOKEN 或 CHAT_ID)")
    
    def send_message(self, message, parse_mode="HTML"):
        """发送 Telegram 消息"""
        if not self.enabled:
            print("⚠️ Telegram 推送未启用,跳过发送")
            return False
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": parse_mode
            }
            
            response = requests.post(url, json=payload, timeout=10)
            result = response.json()
            
            if result.get("ok"):
                print("✅ Telegram 消息发送成功")
                return True
            else:
                print(f"❌ Telegram 消息发送失败: {result.get('description')}")
                return False
                
        except Exception as e:
            print(f"❌ Telegram 推送异常: {e}")
            return False
    
    def send_renewal_result(self, status, old_time, new_time=None, run_time=None):
        """发送续期结果通知"""
        beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
        timestamp = run_time or beijing_time.strftime("%Y-%m-%d %H:%M:%S")
        
        # 构建消息
        message = f"<b>🎮 XServer GAME 续期通知</b>\n\n"
        message += f"🕐 运行时间: <code>{timestamp}</code>\n"
        message += f"🖥 服务器: <code>🇯🇵 Xserver(MC)</code>\n\n"
        
        if status == "Success":
            message += f"📊 续期结果: <b>✅ 成功</b>\n"
            message += f"🕛 旧到期: <code>{old_time}</code>\n"
            message += f"🕡 新到期: <code>{new_time}</code>\n"
        elif status == "Unexpired":
            message += f"📊 续期结果: <b>ℹ️ 未到期</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
            message += f"💡 提示: 剩余时间超过24小时,无需续期\n"
        elif status == "Failed":
            message += f"📊 续期结果: <b>❌ 失败</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
            message += f"⚠️ 请检查日志或手动续期\n"
        else:
            message += f"📊 续期结果: <b>❓ 未知</b>\n"
            message += f"🕛 到期时间: <code>{old_time}</code>\n"
        
        return self.send_message(message)

# =====================================================================
#                        XServer 自动登录类
# =====================================================================

class XServerAutoLogin:
    """XServer GAME 自动登录主类 - Playwright版本"""
    
    def __init__(self):
        """
        初始化 XServer GAME 自动登录器
        使用配置区域的设置
        """
        self.browser = None
        self.context = None
        self.page = None
        self.headless = USE_HEADLESS
        self.email = LOGIN_EMAIL
        self.password = LOGIN_PASSWORD
        self.target_url = TARGET_URL
        self.wait_timeout = WAIT_TIMEOUT
        self.page_load_delay = PAGE_LOAD_DELAY
        self.screenshot_count = 0  # 截图计数器
        
        # 续期状态跟踪
        self.old_expiry_time = None      # 原到期时间
        self.new_expiry_time = None      # 新到期时间
        self.renewal_status = "Unknown"  # 续期状态: Success/Unexpired/Failed/Unknown
        
        # Telegram 推送器
        self.telegram = TelegramNotifier()
    
    
    # =================================================================
    #                       1. 浏览器管理模块
    # =================================================================
        
    async def setup_browser(self):
        """设置并启动 Playwright 浏览器"""
        try:
            playwright = await async_playwright().start()
            
            # 配置浏览器选项
            browser_args = [
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-notifications',
                '--window-size=1920,1080',
                '--lang=ja-JP',
                '--accept-lang=ja-JP,ja,en-US,en'
            ]
            
            # 如果启用代理，添加代理参数
            if USE_PROXY and PROXY_SERVER:
                print(f"🌐 使用代理: {PROXY_SERVER}")
                browser_args.append(f'--proxy-server={PROXY_SERVER}')
            
            # 启动浏览器
            self.browser = await playwright.chromium.launch(
                headless=self.headless,
                args=browser_args
            )
            
            # 创建浏览器上下文配置
            context_options = {
                'viewport': {'width': 1920, 'height': 1080},
                'locale': 'ja-JP',
                'timezone_id': 'Asia/Tokyo',
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            
            # 如果启用代理，添加代理配置到上下文
            if USE_PROXY and PROXY_SERVER:
                context_options['proxy'] = {
                    'server': PROXY_SERVER
                }
            
            # 创建浏览器上下文
            self.context = await self.browser.new_context(**context_options)
            
            # 创建页面
            self.page = await self.context.new_page()
            
            # 应用stealth插件
            await stealth_async(self.page)
            print("✅ Stealth 插件已应用")
            
            if USE_PROXY:
                print(f"✅ Playwright 浏览器初始化成功 (使用代理: {PROXY_SERVER})")
            else:
                print("✅ Playwright 浏览器初始化成功")
            return True
            
        except Exception as e:
            print(f"❌ Playwright 浏览器初始化失败: {e}")
            return False
    
    async def take_screenshot(self, step_name=""):
        """截图功能 - 用于可视化调试"""
        try:
            if self.page:
                self.screenshot_count += 1
                # 使用北京时间(UTC+8)
                beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
                timestamp = beijing_time.strftime("%H%M%S")
                filename = f"step_{self.screenshot_count:02d}_{timestamp}_{step_name}.png"
                
                # 确保文件名安全
                filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                
                await self.page.screenshot(path=filename, full_page=True)
                print(f"📸 截图已保存: {filename}")
                
        except Exception as e:
            print(f"⚠️ 截图失败: {e}")
    
    def validate_config(self):
        """验证配置信息"""
        if not self.email or not self.password:
            print("❌ 邮箱或密码未设置!")
            return False
        
        print("✅ 配置信息验证通过")
        return True
    
    async def cleanup(self):
        """清理资源"""
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            print("🧹 浏览器已关闭")
        except Exception as e:
            print(f"⚠️ 清理资源时出错: {e}")
    
    # =================================================================
    #                       2. 页面导航模块
    # =================================================================
    
    async def navigate_to_login(self):
        """导航到登录页面"""
        try:
            print(f"🌐 正在访问: {self.target_url}")
            await self.page.goto(self.target_url, wait_until='load')
            
            # 等待页面加载
            await self.page.wait_for_selector("body", timeout=self.wait_timeout)
            
            print("✅ 页面加载成功")
            await self.take_screenshot("login_page_loaded")
            return True
            
        except Exception as e:
            print(f"❌ 导航失败: {e}")
            return False
    
    
    # =================================================================
    #                       3. 登录表单处理模块
    # =================================================================
    
    async def find_login_form(self):
        """查找登录表单元素"""
        try:
            print("🔍 正在查找登录表单...")
            
            # 等待页面加载完成
            await asyncio.sleep(self.page_load_delay)
            
            # 查找邮箱输入框
            email_selector = "input[name='memberid']"
            await self.page.wait_for_selector(email_selector, timeout=self.wait_timeout)
            print("✅ 找到邮箱输入框")

            # 查找密码输入框
            password_selector = "input[name='user_password']"
            await self.page.wait_for_selector(password_selector, timeout=self.wait_timeout)
            print("✅ 找到密码输入框")

            # 查找登录按钮
            login_button_selector = "input[value='ログインする']"
            await self.page.wait_for_selector(login_button_selector, timeout=self.wait_timeout)
            print("✅ 找到登录按钮")
            
            return email_selector, password_selector, login_button_selector
            
        except Exception as e:
            print(f"❌ 查找登录表单时出错: {e}")
            return None, None, None
    
    async def human_type(self, selector, text):
        """模拟人类输入行为"""
        for char in text:
            await self.page.type(selector, char, delay=100)
            await asyncio.sleep(0.05)
    
    async def perform_login(self):
        """执行登录操作"""
        try:
            print("🎯 开始执行登录操作...")
            
            # 查找登录表单元素
            email_selector, password_selector, login_button_selector = await self.find_login_form()
            
            if not email_selector or not password_selector:
                return False
            
            print("📝 正在填写登录信息...")
            
            # 模拟人类行为:慢速输入邮箱
            await self.page.fill(email_selector, "")
            await self.human_type(email_selector, self.email)
            print("✅ 邮箱已填写")
            
            # 等待一下,模拟人类思考时间
            await asyncio.sleep(2)
            
            # 模拟人类行为:慢速输入密码
            await self.page.fill(password_selector, "")
            await self.human_type(password_selector, self.password)
            print("✅ 密码已填写")
            
            # 等待一下,模拟人类操作
            await asyncio.sleep(2)
            
            # 提交表单
            if login_button_selector:
                print("🖱️ 点击登录按钮...")
                await self.page.click(login_button_selector)
            else:
                print("⌨️ 使用回车键提交...")
                await self.page.press(password_selector, "Enter")
            
            print("✅ 登录表单已提交")
            
            # 等待页面响应
            await asyncio.sleep(5)
            return True
            
        except Exception as e:
            print(f"❌ 登录操作失败: {e}")
            return False
    
    
    # =================================================================
    #                       4. 登录结果处理模块
    # =================================================================
    
    async def handle_login_result(self):
        """处理登录结果"""
        try:
            print("🔍 正在检查登录结果...")
            
            # 等待页面加载
            await asyncio.sleep(3)
            
            current_url = self.page.url
            print(f"🔍 当前URL: {current_url}")
            
            # 简单直接:只判断是否跳转到成功页面
            success_url = "https://secure.xserver.ne.jp/xapanel/xmgame/index"
            
            if current_url == success_url:
                print("✅ 登录成功!已跳转到XServer GAME管理页面")
                
                # 等待页面加载完成
                print("⏰ 等待页面加载完成...")
                await asyncio.sleep(3)
                
                # 查找并点击"ゲーム管理"按钮
                print("🔍 正在查找ゲーム管理按钮...")
                try:
                    game_button_selector = "a:has-text('ゲーム管理')"
                    await self.page.wait_for_selector(game_button_selector, timeout=self.wait_timeout)
                    print("✅ 找到ゲーム管理按钮")
                    
                    # 点击ゲーム管理按钮
                    print("🖱️ 正在点击ゲーム管理按钮...")
                    await self.page.click(game_button_selector)
                    print("✅ 已点击ゲーム管理按钮")
                    
                    # 等待页面跳转（可能有中间跳转页面）
                    print("⏰ 等待页面跳转...")
                    await asyncio.sleep(3)
                    
                    # 检查是否在中间跳转页
                    current_url = self.page.url
                    if "jumpvps" in current_url:
                        print("🔄 检测到中间跳转页面 (jumpvps)，等待最终跳转...")
                        # 等待跳转到最终页面，最多等待15秒
                        for i in range(15):
                            await asyncio.sleep(1)
                            final_url = self.page.url
                            if "xmgame/game/index" in final_url:
                                print(f"✅ 成功跳转到游戏管理页面 (耗时 {i+1} 秒)")
                                break
                            if i == 14:
                                print("⚠️ 等待跳转超时，继续执行...")
                    else:
                        # 没有中间跳转，直接检查
                        await asyncio.sleep(3)
                    
                    # 验证最终URL
                    final_url = self.page.url
                    print(f"🔍 最终页面URL: {final_url}")
                    
                    expected_game_url = "https://secure.xserver.ne.jp/xmgame/game/index"
                    if expected_game_url in final_url:
                        print("✅ 成功到达游戏管理页面")
                        await self.take_screenshot("game_page_loaded")
                        
                        # 获取服务器时间信息
                        await self.get_server_time_info()
                    else:
                        print(f"⚠️ 当前URL不是预期的游戏管理页面")
                        print(f"   预期: {expected_game_url}")
                        print(f"   实际: {final_url}")
                        print("ℹ️ 尝试继续执行...")
                        await self.take_screenshot("game_page_unexpected_url")
                        
                        # 即使URL不完全匹配，也尝试获取服务器信息
                        await self.get_server_time_info()
                        
                except Exception as e:
                    print(f"❌ 查找或点击ゲーム管理按钮时出错: {e}")
                    await self.take_screenshot("game_button_error")
                
                return True
            else:
                print(f"❌ 登录失败!当前URL不是预期的成功页面")
                print(f"   预期URL: {success_url}")
                print(f"   实际URL: {current_url}")
                return False
            
        except Exception as e:
            print(f"❌ 检查登录结果时出错: {e}")
            return False
            
    # =================================================================
    #                    5A. 服务器信息获取模块
    # =================================================================
    
    async def get_server_time_info(self):
        """获取服务器时间信息"""
        try:
            print("🕒 正在获取服务器时间信息...")
            
            # 等待页面加载完成
            await asyncio.sleep(3)
            
            # 使用已验证有效的选择器
            try:
                elements = await self.page.locator("text=/残り\\d+時間\\d+分/").all()
                
                for element in elements:
                    element_text = await element.text_content()
                    element_text = element_text.strip() if element_text else ""
                    
                    # 只处理包含时间信息且文本不太长的元素
                    if element_text and len(element_text) < 200 and "残り" in element_text and "時間" in element_text:
                        print(f"✅ 找到时间元素: {element_text}")
                        
                        # 提取剩余时间
                        remaining_match = re.search(r'残り(\d+時間\d+分)', element_text)
                        if remaining_match:
                            remaining_raw = remaining_match.group(1)
                            remaining_formatted = self.format_remaining_time(remaining_raw)
                            print(f"⏰ 剩余时间: {remaining_formatted}")
                        
                        # 提取到期时间
                        expiry_match = re.search(r'\((\d{4}-\d{2}-\d{2})まで\)', element_text)
                        if expiry_match:
                            expiry_raw = expiry_match.group(1)
                            expiry_formatted = self.format_expiry_date(expiry_raw)
                            print(f"📅 到期时间: {expiry_formatted}")
                            # 记录原到期时间
                            self.old_expiry_time = expiry_formatted
                        
                        break
                        
            except Exception as e:
                print(f"❌ 获取时间信息时出错: {e}")
            
            # 点击升级按钮
            await self.click_upgrade_button()
            
        except Exception as e:
            print(f"❌ 获取服务器时间信息失败: {e}")
    
    def format_remaining_time(self, time_str):
        """格式化剩余时间"""
        # 移除"残り"前缀,只保留时间部分
        return time_str  # 例如: "30時間57分"
    
    def format_expiry_date(self, date_str):
        """格式化到期时间"""
        # 直接返回日期,移除括号和"まで"
        return date_str  # 例如: "2025-09-24"
    
    # =================================================================
    #                    5B. 续期页面导航模块
    # =================================================================
    
    async def click_upgrade_button(self):
        """点击升级延长按钮"""
        try:
            print("📄 正在查找アップグレード・期限延長按钮...")
            
            upgrade_selector = "a:has-text('アップグレード・期限延長')"
            await self.page.wait_for_selector(upgrade_selector, timeout=self.wait_timeout)
            print("✅ 找到アップグレード・期限延長按钮")
            
            # 点击按钮
            await self.page.click(upgrade_selector)
            print("✅ 已点击アップグレード・期限延長按钮")
            
            # 等待页面跳转
            await asyncio.sleep(5)
            
            # 验证URL并检查限制信息
            await self.verify_upgrade_page()
            
        except Exception as e:
            print(f"❌ 点击升级按钮失败: {e}")
    
    async def verify_upgrade_page(self):
        """验证升级页面"""
        try:
            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/index"
            
            print(f"🔍 升级页面URL: {current_url}")
            
            if expected_url in current_url:
                print("✅ 成功跳转到升级页面")
                
                # 检查延长限制信息
                await self.check_extension_restriction()
            else:
                print(f"❌ 升级页面跳转失败")
                print(f"   预期URL: {expected_url}")
                print(f"   实际URL: {current_url}")
                
        except Exception as e:
            print(f"❌ 验证升级页面失败: {e}")
    
    async def check_extension_restriction(self):
        """检查期限延长限制信息"""
        try:
            print("🔍 正在检测期限延长限制提示...")
            
            # 查找限制信息
            restriction_selector = "text=/残り契約時間が24時間を切るまで、期限の延長は行えません/"
            
            try:
                element = await self.page.wait_for_selector(restriction_selector, timeout=5000)
                restriction_text = await element.text_content()
                print(f"✅ 找到期限延长限制信息")
                print(f"🔍 限制信息: {restriction_text}")
                # 设置状态为未到期
                self.renewal_status = "Unexpired"
                return True  # 有限制,不能续期
                
            except Exception:
                print("ℹ️ 未找到期限延长限制信息,可以进行延长操作")
                # 没有限制信息,执行续期操作
                await self.perform_extension_operation()
                return False  # 无限制,可以续期
                
        except Exception as e:
            print(f"❌ 检测期限延长限制失败: {e}")
            return True  # 出错时默认认为有限制
    
    # =================================================================
    #                    5C. 续期操作执行模块
    # =================================================================
    
    async def perform_extension_operation(self):
        """执行期限延长操作"""
        try:
            print("📄 开始执行期限延长操作...")
            
            # 查找"期限を延長する"按钮
            await self.click_extension_button()
            
        except Exception as e:
            print(f"❌ 执行期限延长操作失败: {e}")
    
    async def click_extension_button(self):
        """点击期限延长按钮"""
        try:
            print("🔍 正在查找'期限を延長する'按钮...")
            
            # 使用有效的选择器
            extension_selector = "a:has-text('期限を延長する')"
            
            # 等待并点击按钮
            await self.page.wait_for_selector(extension_selector, timeout=self.wait_timeout)
            print("✅ 找到'期限を延長する'按钮")
            
            # 点击按钮
            await self.page.click(extension_selector)
            print("✅ 已点击'期限を延長する'按钮")
            
            # 等待页面跳转
            print("⏰ 等待页面跳转...")
            await asyncio.sleep(5)
            
            # 验证是否跳转到input页面
            await self.verify_extension_input_page()
            return True
            
        except Exception as e:
            print(f"❌ 点击期限延长按钮失败: {e}")
            return False
    
    async def verify_extension_input_page(self):
        """验证是否成功跳转到期限延长输入页面"""
        try:
            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/input"
            
            print(f"🔍 当前页面URL: {current_url}")
            
            if expected_url in current_url:
                print("🎉 成功跳转到期限延长输入页面!")
                await self.take_screenshot("extension_input_page")
                
                # 继续执行确认操作
                await self.click_confirmation_button()
                return True
            else:
                print(f"❌ 页面跳转失败")
                print(f"   预期URL: {expected_url}")
                print(f"   实际URL: {current_url}")
                return False
            
        except Exception as e:
            print(f"❌ 验证期限延长输入页面失败: {e}")
            return False
            
    async def click_confirmation_button(self):
        """点击確認画面に進む按钮"""
        try:
            print("🔍 正在查找'確認画面に進む'按钮...")
            
            # 使用button元素的选择器
            confirmation_selector = "button[type='submit']:has-text('確認画面に進む')"
            
            # 等待并点击按钮
            await self.page.wait_for_selector(confirmation_selector, timeout=self.wait_timeout)
            print("✅ 找到'確認画面に進む'按钮")
            
            # 点击按钮
            await self.page.click(confirmation_selector)
            print("✅ 已点击'確認画面に進む'按钮")
            
            # 等待页面跳转
            print("⏰ 等待页面跳转...")
            await asyncio.sleep(5)
            
            # 验证是否跳转到conf页面
            await self.verify_extension_conf_page()
            return True
            
        except Exception as e:
            print(f"❌ 点击確認画面に進む按钮失败: {e}")
            return False
            
    async def verify_extension_conf_page(self):
        """验证是否成功跳转到期限延长确认页面"""
        try:
            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/conf"
            
            print(f"🔍 当前页面URL: {current_url}")
            
            if expected_url in current_url:
                print("🎉 成功跳转到期限延长确认页面!")
                await self.take_screenshot("extension_conf_page")
                
                # 记录续期后的时间信息
                await self.record_extension_time()
                
                # 查找期限延长按钮
                await self.find_final_extension_button()
                
                return True
            else:
                print(f"❌ 页面跳转失败")
                print(f"   预期URL: {expected_url}")
                print(f"   实际URL: {current_url}")
                return False
            
        except Exception as e:
            print(f"❌ 验证期限延长确认页面失败: {e}")
            return False
    
    async def record_extension_time(self):
        """记录续期后的时间信息"""
        try:
            print("📅 正在获取续期后的时间信息...")
            
            # 使用有效的选择器
            time_selector = "tr:has(th:has-text('延長後の期限'))"
            
            # 等待并获取时间信息
            time_element = await self.page.wait_for_selector(time_selector, timeout=self.wait_timeout)
            print("✅ 找到续期后时间信息")
            
            # 获取整行,然后提取td内容
            td_element = await time_element.query_selector("td")
            if td_element:
                extension_time = await td_element.text_content()
                extension_time = extension_time.strip()
                print(f"📅 续期后的期限: {extension_time}")
                # 记录新到期时间
                self.new_expiry_time = extension_time
            else:
                print("❌ 未找到时间内容")
            
        except Exception as e:
            print(f"❌ 记录续期后时间失败: {e}")
    
    async def find_final_extension_button(self):
        """查找并点击最终的期限延长按钮"""
        try:
            print("🔍 正在查找最终的'期限を延長する'按钮...")
            
            # 基于HTML属性查找按钮
            final_button_selector = "button[type='submit']:has-text('期限を延長する')"
            
            # 等待按钮出现
            await self.page.wait_for_selector(final_button_selector, timeout=self.wait_timeout)
            print("✅ 找到最终的'期限を延長する'按钮")
            
            # 点击按钮执行最终续期
            await self.page.click(final_button_selector)
            print("✅ 已点击最终续期按钮")
            
            # 等待页面跳转
            print("⏰ 等待续期操作完成...")
            await asyncio.sleep(5)
            
            # 验证续期结果
            await self.verify_extension_success()
            
            return True
            
        except Exception as e:
            print(f"❌ 执行最终期限延长操作失败: {e}")
            return False
            
    async def verify_extension_success(self):
        """验证续期操作是否成功"""
        try:
            print("🔍 正在验证续期操作结果...")
            
            current_url = self.page.url
            expected_url = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/do"
            
            print(f"🔍 当前页面URL: {current_url}")
            
            # 检查条件1:URL是否跳转到do页面
            url_success = expected_url in current_url
            
            # 检查条件2:是否有成功提示文字
            text_success = False
            try:
                success_text_selector = "p:has-text('期限を延長しました。')"
                await self.page.wait_for_selector(success_text_selector, timeout=5000)
                success_text = await self.page.query_selector(success_text_selector)
                if success_text:
                    text_content = await success_text.text_content()
                    print(f"✅ 找到成功提示文字: {text_content.strip()}")
                    text_success = True
            except Exception:
                print("ℹ️ 未找到成功提示文字")
            
            # 任意一项满足即为成功
            if url_success or text_success:
                print("🎉 续期操作成功!")
                if url_success:
                    print(f"✅ URL验证成功: {current_url}")
                if text_success:
                    print("✅ 成功提示文字验证成功")
                
                # 设置状态为成功
                self.renewal_status = "Success"
                await self.take_screenshot("extension_success")
                return True
            else:
                print("❌ 续期操作可能失败")
                print(f"   当前URL: {current_url}")
                print(f"   期望URL: {expected_url}")
                # 设置状态为失败
                self.renewal_status = "Failed"
                await self.take_screenshot("extension_failed")
                return False
            
        except Exception as e:
            print(f"❌ 验证续期结果失败: {e}")
            # 设置状态为失败
            self.renewal_status = "Failed"
            return False
        
    # =================================================================
    #                    5D. 结果记录与报告模块
    # =================================================================
    
    def generate_readme(self):
        """生成README.md文件记录续期情况"""
        try:
            print("📝 正在生成README.md文件...")
            
            # 获取当前时间
            # 使用北京时间(UTC+8)
            beijing_time = datetime.datetime.now(timezone(timedelta(hours=8)))
            current_time = beijing_time.strftime("%Y-%m-%d %H:%M:%S")
            
            # 根据状态生成不同的内容
            readme_content = f"**最后运行时间**: `{current_time}`\n\n"
            readme_content += "**运行结果**: <br>\n"
            readme_content += "🖥️服务器:`🇯🇵Xserver(MC)`<br>\n"
            
            # 根据续期状态生成对应的结果
            if self.renewal_status == "Success":
                readme_content += "📊续期结果:✅Success<br>\n"
                readme_content += f"🕛️旧到期时间: `{self.old_expiry_time or 'Unknown'}`<br>\n"
                readme_content += f"🕡️新到期时间: `{self.new_expiry_time or 'Unknown'}`<br>\n"
            elif self.renewal_status == "Unexpired":
                readme_content += "📊续期结果:ℹ️Unexpired<br>\n"
                readme_content += f"🕛️旧到期时间: `{self.old_expiry_time or 'Unknown'}`<br>\n"
            elif self.renewal_status == "Failed":
                readme_content += "📊续期结果:❌Failed<br>\n"
                readme_content += f"🕛️旧到期时间: `{self.old_expiry_time or 'Unknown'}`<br>\n"
            else:
                readme_content += "📊续期结果:❓Unknown<br>\n"
                readme_content += f"🕛️旧到期时间: `{self.old_expiry_time or 'Unknown'}`<br>\n"
            
            # 写入README.md文件
            with open("README.md", "w", encoding="utf-8") as f:
                f.write(readme_content)
            
            print("✅ README.md文件生成成功")
            print(f"📄 续期状态: {self.renewal_status}")
            print(f"📅 原到期时间: {self.old_expiry_time or 'Unknown'}")
            if self.new_expiry_time:
                print(f"📅 新到期时间: {self.new_expiry_time}")
            
            # 推送到 Telegram
            self.push_to_telegram(current_time)
            
        except Exception as e:
            print(f"❌ 生成README.md文件失败: {e}")
    
    def push_to_telegram(self, run_time=None):
        """推送结果到 Telegram"""
        try:
            print("📱 正在推送结果到 Telegram...")
            
            result = self.telegram.send_renewal_result(
                status=self.renewal_status,
                old_time=self.old_expiry_time or "Unknown",
                new_time=self.new_expiry_time,
                run_time=run_time
            )
            
            if result:
                print("✅ Telegram 推送成功")
            else:
                print("⚠️ Telegram 推送失败或未启用")
                
        except Exception as e:
            print(f"❌ Telegram 推送异常: {e}")
    
    # =================================================================
    #                       6. 主流程控制模块
    # =================================================================
    
    async def run(self):
        """运行自动登录流程"""
        try:
            print("🚀 开始 XServer GAME 自动登录流程...")
            
            # 步骤1:验证配置
            if not self.validate_config():
                return False
            
            # 步骤2:设置浏览器
            if not await self.setup_browser():
                return False
            
            # 步骤3:导航到登录页面
            if not await self.navigate_to_login():
                return False
            
            # 步骤4:执行登录操作
            if not await self.perform_login():
                return False
            
            # 步骤5:检查登录结果
            if not await self.handle_login_result():
                print("⚠️ 登录可能失败,请检查邮箱和密码是否正确")
                return False
            
            print("🎉 XServer GAME 自动登录流程完成!")
            await self.take_screenshot("login_completed")
            
            # 生成README.md文件和推送Telegram
            self.generate_readme()
            
            # 保持浏览器打开一段时间以便查看结果
            print("⏰ 浏览器将在 10 秒后关闭...")
            await asyncio.sleep(10)
            
            return True
            
        except Exception as e:
            print(f"❌ 自动登录流程出错: {e}")
            # 即使出错也生成README文件和推送Telegram
            self.generate_readme()
            return False
    
        finally:
            await self.cleanup()


# =====================================================================
#                          主程序入口
# =====================================================================

async def main():
    """主函数"""
    print("=" * 60)
    print("XServer GAME 自动登录脚本 - Playwright版本")
    print("基于 Playwright + stealth")
    print("=" * 60)
    print()
    
    # 显示当前配置
    print("📋 当前配置:")
    print(f"   XServer邮箱: {LOGIN_EMAIL}")
    print(f"   XServer密码: {'*' * len(LOGIN_PASSWORD) if LOGIN_PASSWORD else 'None'}")
    print(f"   目标网站: {TARGET_URL}")
    print(f"   无头模式: {USE_HEADLESS}")
    if USE_PROXY and PROXY_SERVER:
        print(f"   代理服务器: {PROXY_SERVER}")
    else:
        print(f"   代理服务器: 未使用")
    print()
    
    # 显示Telegram配置
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        print("📱 Telegram推送配置:")
        print(f"   Bot Token: {TELEGRAM_BOT_TOKEN[:10]}{'*' * (len(TELEGRAM_BOT_TOKEN) - 10) if len(TELEGRAM_BOT_TOKEN) > 10 else ''}")
        print(f"   Chat ID: {TELEGRAM_CHAT_ID}")
    else:
        print("ℹ️ Telegram推送未配置(可选功能)")
    print()
    
    # 确认配置
    if not LOGIN_EMAIL or not LOGIN_PASSWORD or LOGIN_EMAIL == "your_email@example.com" or LOGIN_PASSWORD == "your_password":
        print("❌ 请先设置正确的邮箱和密码!")
        print("   可以通过环境变量 XSERVER_EMAIL 和 XSERVER_PASSWORD 设置")
        return
    
    print("🚀 配置验证通过,自动开始登录...")
    
    # 创建并运行自动登录器
    auto_login = XServerAutoLogin()
    
    success = await auto_login.run()
    
    if success:
        print("✅ 登录流程执行成功!")
        exit(0)
    else:
        print("❌ 登录流程执行失败!")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
