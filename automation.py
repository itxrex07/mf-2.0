import asyncio
import aiohttp
import logging
from datetime import datetime, timedelta
from db import (
    get_automation_settings, set_automation_settings, get_automation_accounts,
    update_automation_last_request, get_tokens, is_already_sent, add_sent_id
)
from friend_requests import fetch_users, process_users
from lounge import fetch_lounge_users, process_single_lounge_user
from chatroom import send_message_to_everyone

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class AutomationManager:
    def __init__(self, bot):
        self.bot = bot
        self.running_automations = {}
        self.lounge_cache = {}  # Cache to track seen users
        
    async def start_automation(self, user_id):
        """Start automation for a user"""
        if user_id in self.running_automations:
            return False, "Automation already running"
        
        settings = get_automation_settings(user_id)
        if not settings.get("enabled"):
            return False, "Automation is disabled"
        
        automation_accounts = get_automation_accounts(user_id)
        if not automation_accounts:
            return False, "No accounts enabled for automation"
        
        self.running_automations[user_id] = True
        asyncio.create_task(self._automation_loop(user_id))
        return True, "Automation started successfully"
    
    async def stop_automation(self, user_id):
        """Stop automation for a user"""
        if user_id in self.running_automations:
            self.running_automations[user_id] = False
            return True, "Automation stopped"
        return False, "Automation not running"
    
    async def _automation_loop(self, user_id):
        """Main automation loop"""
        logger.info(f"Starting automation loop for user {user_id}")
        
        while self.running_automations.get(user_id, False):
            try:
                settings = get_automation_settings(user_id)
                if not settings.get("enabled"):
                    break
                
                # Check for friend requests (24-hour cycle)
                await self._check_friend_requests(user_id, settings)
                
                # Check lounge for new users
                if settings.get("lounge_enabled"):
                    await self._check_lounge_users(user_id, settings)
                
                # Wait before next check
                check_interval = settings.get("lounge_check_interval", 5) * 60  # Convert to seconds
                await asyncio.sleep(check_interval)
                
            except Exception as e:
                logger.error(f"Error in automation loop for user {user_id}: {e}")
                await asyncio.sleep(60)  # Wait 1 minute before retrying
        
        # Clean up
        if user_id in self.running_automations:
            del self.running_automations[user_id]
        logger.info(f"Automation loop ended for user {user_id}")
    
    async def _check_friend_requests(self, user_id, settings):
        """Check if it's time to send friend requests"""
        last_request = settings.get("last_friend_request")
        interval_hours = settings.get("friend_request_interval", 24)
        
        if last_request:
            if isinstance(last_request, str):
                last_request = datetime.fromisoformat(last_request)
            time_since_last = datetime.utcnow() - last_request
            if time_since_last.total_seconds() < interval_hours * 3600:
                return  # Not time yet
        
        # Time to send friend requests
        automation_accounts = get_automation_accounts(user_id)
        if not automation_accounts:
            return
        
        logger.info(f"Starting automated friend requests for user {user_id}")
        
        try:
            # Send notification to user
            await self.bot.send_message(
                user_id,
                "🤖 <b>Automation</b>\n\n"
                "Starting automated friend requests...",
                parse_mode="HTML"
            )
            
            total_added = 0
            async with aiohttp.ClientSession() as session:
                for account in automation_accounts:
                    token = account["token"]
                    account_name = account.get("name", "Unknown")
                    
                    try:
                        # Fetch users for this account
                        users = await fetch_users(session, token)
                        if users:
                            # Process users (limit to 10 per account to avoid spam)
                            limited_users = users[:10]
                            _, added_count, _ = await process_users(
                                session, limited_users, token, user_id, 
                                self.bot, None, account_name
                            )
                            total_added += added_count
                            
                            # Small delay between accounts
                            await asyncio.sleep(5)
                            
                    except Exception as e:
                        logger.error(f"Error processing account {account_name}: {e}")
            
            # Update last request time
            update_automation_last_request(user_id)
            
            # Send completion notification
            await self.bot.send_message(
                user_id,
                f"🤖 <b>Automation Complete</b>\n\n"
                f"Automated friend requests finished.\n"
                f"Total added: {total_added}",
                parse_mode="HTML"
            )
            
        except Exception as e:
            logger.error(f"Error in automated friend requests for user {user_id}: {e}")
    
    async def _check_lounge_users(self, user_id, settings):
        """Check lounge for new users and send messages"""
        automation_accounts = get_automation_accounts(user_id)
        if not automation_accounts:
            return
        
        lounge_message = settings.get("lounge_message", "Hello! Nice to meet you! 😊")
        chatroom_enabled = settings.get("chatroom_enabled", False)
        chatroom_message = settings.get("chatroom_message", "Hi there! How are you doing? 🌟")
        chatroom_delay = settings.get("chatroom_delay", 30) * 60  # Convert to seconds
        
        # Initialize cache for this user if not exists
        if user_id not in self.lounge_cache:
            self.lounge_cache[user_id] = set()
        
        for account in automation_accounts:
            token = account["token"]
            account_name = account.get("name", "Unknown")
            
            try:
                # Fetch lounge users
                lounge_users = await fetch_lounge_users(token)
                if not lounge_users:
                    continue
                
                new_users = []
                for user_data in lounge_users:
                    user_obj = user_data.get("user", {})
                    user_lounge_id = user_obj.get("_id")
                    
                    if user_lounge_id and user_lounge_id not in self.lounge_cache[user_id]:
                        new_users.append(user_data)
                        self.lounge_cache[user_id].add(user_lounge_id)
                
                if new_users:
                    logger.info(f"Found {len(new_users)} new lounge users for {account_name}")
                    
                    # Send lounge messages to new users
                    for user_data in new_users[:5]:  # Limit to 5 new users per check
                        try:
                            success = await process_single_lounge_user(
                                token, user_data, lounge_message, user_id, False
                            )
                            
                            if success and chatroom_enabled:
                                # Schedule chatroom message
                                asyncio.create_task(
                                    self._send_delayed_chatroom_message(
                                        user_id, token, chatroom_message, chatroom_delay
                                    )
                                )
                            
                            await asyncio.sleep(2)  # Small delay between messages
                            
                        except Exception as e:
                            logger.error(f"Error sending lounge message: {e}")
                
            except Exception as e:
                logger.error(f"Error checking lounge for {account_name}: {e}")
    
    async def _send_delayed_chatroom_message(self, user_id, token, message, delay):
        """Send chatroom message after delay"""
        await asyncio.sleep(delay)
        
        try:
            # Send chatroom message using existing function
            await send_message_to_everyone(
                token, message, spam_enabled=True
            )
            
            # Notify user
            await self.bot.send_message(
                user_id,
                "🤖 <b>Automation</b>\n\n"
                "Automated chatroom messages sent.",
                parse_mode="HTML"
            )
            
        except Exception as e:
            logger.error(f"Error sending delayed chatroom message: {e}")
    
    def is_running(self, user_id):
        """Check if automation is running for user"""
        return self.running_automations.get(user_id, False)

# Global automation manager instance
automation_manager = None

def get_automation_manager(bot):
    """Get or create automation manager instance"""
    global automation_manager
    if automation_manager is None:
        automation_manager = AutomationManager(bot)
    return automation_manager