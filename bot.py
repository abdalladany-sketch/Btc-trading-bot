import time
import json
import hmac
import base64
import logging
import requests
from datetime import datetime, timezone

# ================= Configuration =================
import os

OKX_API_KEY = os.environ.get("OKX_API_KEY")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE")
SYMBOL = "BTC-USDT-SWAP"
TIME_FRAME = "4H"
RISK_PERCENT = 0.01  # 1% risk per trade
TRADING_FEE = 0.0005  # 0.05% per transaction
MIN_SL_PERCENT = 0.001  # 0.1% minimum stop loss percentage

BASE_URL = "https://www.okx.com"
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class OKXClient:
    def __init__(self, api_key, secret_key, passphrase):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.base_url = BASE_URL
        
    def _generate_signature(self, timestamp, method, request_path, body):
        if body is None:
            body = ''
        else:
            body = json.dumps(body)
        
        message = timestamp + method.upper() + request_path + body
        mac = hmac.new(
            self.secret_key.encode('utf-8'),
            message.encode('utf-8'),
            digestmod='sha256'
        )
        return base64.b64encode(mac.digest()).decode('utf-8')
    
    def _make_request(self, method, endpoint, body=None):
        timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        request_path = f"/api/v5{endpoint}"
        signature = self._generate_signature(timestamp, method, request_path, body)
        
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json"
        }
        
        url = self.base_url + request_path
        response = requests.request(method, url, headers=headers, json=body)
        return response.json()
    
    def get_account_balance(self):
        endpoint = "/account/balance"
        response = self._make_request("GET", endpoint)
        if response.get("code") == "0":
            details = response.get("data", [{}])[0].get("details", [])
            for asset in details:
                if asset.get("ccy") == "USDT":
                    return float(asset.get("eq", 0))
        return 0
    
    def get_candlesticks(self, inst_id, bar, limit=2):
        endpoint = f"/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
        response = self._make_request("GET", endpoint)
        if response.get("code") == "0":
            return response.get("data", [])
        raise Exception(f"Failed to fetch candles: {response.get('msg')}")
    
    def get_ticker(self, inst_id):
        endpoint = f"/market/ticker?instId={inst_id}"
        response = self._make_request("GET", endpoint)
        if response.get("code") == "0":
            data = response.get("data", [{}])[0]
            return float(data.get("last", 0))
        return 0
    
    def place_market_order(self, inst_id, side, sz):
        body = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": side,
            "ordType": "market",
            "sz": str(sz),
        }
        
        response = self._make_request("POST", "/trade/order", body)
        if response.get("code") == "0":
            return response["data"][0]["ordId"]
        else:
            raise Exception(f"Market order failed: {response.get('msg')}")
    
    def set_leverage(self, inst_id, lever):
        body = {"instId": inst_id, "lever": str(lever), "mgnMode": "cross"}
        response = self._make_request("POST", "/account/set-leverage", body)
        return response.get("code") == "0"


class GoldFuturesBot:
    def __init__(self):
        self.client = OKXClient(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE)
        self.current_candle_ts = None
        self.current_candle_high = None
        self.current_candle_low = None
        
        # Trade state tracking
        self.high_entry_used = False
        self.low_entry_used = False
        self.active_position = None
        self.position_candle_ts = None
        self.lot_size = 0.001
        
        # Statistics
        self.total_fees_paid = 0
        self.total_trades = 0
        self.rejected_trades = 0
        
        # Get instrument info
        try:
            instrument = self.get_instrument_info(SYMBOL)
            if instrument:
                self.lot_size = float(instrument.get("lotSz", "0.0001"))
                logger.info(f"Lot size: {self.lot_size}")
        except:
            logger.warning(f"Using default lot size: {self.lot_size}")
        
        # Set leverage
        try:
            self.client.set_leverage(SYMBOL, "100")
            logger.info("Leverage set to 100x")
        except Exception as e:
            logger.warning(f"Failed to set leverage: {e}")
    
    def get_instrument_info(self, inst_id):
        endpoint = f"/public/instruments?instType=SWAP&instId={inst_id}"
        response = self.client._make_request("GET", endpoint)
        if response.get("code") == "0":
            data = response.get("data", [])
            if data:
                return data[0]
        return None
    
    def round_to_lot_size(self, size):
        if self.lot_size <= 0:
            return round(size, 3)
        lots = max(1, int(round(size / self.lot_size)))
        return lots * self.lot_size
    
    def calculate_position_size(self, account_balance, sl_distance, entry_price):
        """Calculate position size based on 1% risk and actual SL percentage"""
        if account_balance <= 0 or sl_distance <= 0:
            return self.lot_size
        
        # Calculate actual SL percentage
        sl_percent = (sl_distance / entry_price) * 100
        
        # Adjust position size based on SL percentage vs minimum
        # If SL is wider than minimum, reduce position size to keep 1% risk
        risk_amount = account_balance * RISK_PERCENT
        position_size = risk_amount / sl_distance
        position_size = self.round_to_lot_size(position_size)
        position_size = max(self.lot_size, position_size)
        position_size = min(position_size, 1000)
        
        return position_size
    
    def fetch_latest_4h_candle(self):
        try:
            candles = self.client.get_candlesticks(SYMBOL, TIME_FRAME, limit=2)
            if not candles or len(candles) < 2:
                return None
            return candles[1]
        except Exception as e:
            logger.error(f"Failed to fetch candle: {e}")
            return None
    
    def calculate_risk_params(self, high, low):
        rng = high - low
        sl_distance = rng * 0.10  # 10% of range
        tp_distance = sl_distance * 2.5  # 2.5x stop loss
        return rng, sl_distance, tp_distance
    
    def check_sl_percentage(self, entry_price, sl_distance):
        """
        Check if stop loss percentage meets minimum requirement
        Minimum SL is 0.1% to avoid noise and fees
        
        Returns: (is_valid, sl_percent, message)
        """
        sl_percent = (sl_distance / entry_price) * 100
        min_sl_percent = MIN_SL_PERCENT * 100
        
        if sl_percent < min_sl_percent:
            message = (f"  ❌ SL {sl_percent:.3f}% is below minimum {min_sl_percent:.1f}% - "
                      f"Too tight, would get stopped by fees/noise")
            return False, sl_percent, message
        else:
            message = f"  ✅ SL {sl_percent:.3f}% meets minimum {min_sl_percent:.1f}% requirement"
            return True, sl_percent, message
    
    def check_and_execute_entries(self, current_price, position_size):
        """Check for entry signals with minimum SL protection"""
        
        # LONG Entry
        if current_price >= self.current_candle_high and not self.high_entry_used and not self.active_position:
            logger.info(f"\n🚀 LONG SIGNAL - Price {current_price:.2f} reached High {self.current_candle_high:.2f}")
            
            # Calculate SL distance from the candle range
            rng, sl_distance, tp_distance = self.calculate_risk_params(
                self.current_candle_high, self.current_candle_low
            )
            entry_price = current_price
            
            # Check if SL meets minimum percentage requirement
            is_valid_sl, sl_percent, sl_message = self.check_sl_percentage(entry_price, sl_distance)
            logger.info(sl_message)
            
            if not is_valid_sl:
                logger.info(f"❌ LONG trade REJECTED - Stop loss too tight")
                logger.info(f"   Current SL: {sl_percent:.3f}% | Minimum required: {MIN_SL_PERCENT*100:.1f}%")
                self.high_entry_used = True  # Mark as used to avoid repeated rejections
                self.rejected_trades += 1
                return False
            
            # Calculate actual position size based on this SL
            actual_position_size = self.calculate_position_size(
                self.last_balance, sl_distance, entry_price
            )
            
            try:
                order_id = self.client.place_market_order(SYMBOL, "buy", actual_position_size)
                logger.info(f"✅ LONG market order executed: {order_id}")
                
                self.high_entry_used = True
                sl_price = entry_price - sl_distance
                tp_price = entry_price + tp_distance
                
                # Calculate fee impact
                position_value = actual_position_size * entry_price
                total_fees = position_value * TRADING_FEE * 2
                
                self.active_position = {
                    "side": "long",
                    "entry": entry_price,
                    "size": actual_position_size,
                    "sl": sl_price,
                    "tp": tp_price,
                    "sl_distance": sl_distance,
                    "sl_percent": sl_percent,
                    "tp_percent": (tp_distance / entry_price) * 100,
                    "fees": total_fees,
                    "candle_ts": self.current_candle_ts
                }
                self.position_candle_ts = self.current_candle_ts
                
                logger.info(f"  Entry: {entry_price:.2f} | SL: {sl_price:.2f} ({sl_percent:.3f}%) | TP: {tp_price:.2f}")
                logger.info(f"  Risk: ${actual_position_size * sl_distance:.2f} (1% of balance)")
                logger.info(f"  Est. Fees (0.1%): ${total_fees:.2f}")
                return True
            except Exception as e:
                logger.error(f"❌ Failed to execute LONG: {e}")
        
        # SHORT Entry
        elif current_price <= self.current_candle_low and not self.low_entry_used and not self.active_position:
            logger.info(f"\n🚀 SHORT SIGNAL - Price {current_price:.2f} reached Low {self.current_candle_low:.2f}")
            
            # Calculate SL distance from the candle range
            rng, sl_distance, tp_distance = self.calculate_risk_params(
                self.current_candle_high, self.current_candle_low
            )
            entry_price = current_price
            
            # Check if SL meets minimum percentage requirement
            is_valid_sl, sl_percent, sl_message = self.check_sl_percentage(entry_price, sl_distance)
            logger.info(sl_message)
            
            if not is_valid_sl:
                logger.info(f"❌ SHORT trade REJECTED - Stop loss too tight")
                logger.info(f"   Current SL: {sl_percent:.3f}% | Minimum required: {MIN_SL_PERCENT*100:.1f}%")
                self.low_entry_used = True  # Mark as used to avoid repeated rejections
                self.rejected_trades += 1
                return False
            
            # Calculate actual position size based on this SL
            actual_position_size = self.calculate_position_size(
                self.last_balance, sl_distance, entry_price
            )
            
            try:
                order_id = self.client.place_market_order(SYMBOL, "sell", actual_position_size)
                logger.info(f"✅ SHORT market order executed: {order_id}")
                
                self.low_entry_used = True
                sl_price = entry_price + sl_distance
                tp_price = entry_price - tp_distance
                
                # Calculate fee impact
                position_value = actual_position_size * entry_price
                total_fees = position_value * TRADING_FEE * 2
                
                self.active_position = {
                    "side": "short",
                    "entry": entry_price,
                    "size": actual_position_size,
                    "sl": sl_price,
                    "tp": tp_price,
                    "sl_distance": sl_distance,
                    "sl_percent": sl_percent,
                    "tp_percent": (tp_distance / entry_price) * 100,
                    "fees": total_fees,
                    "candle_ts": self.current_candle_ts
                }
                self.position_candle_ts = self.current_candle_ts
                
                logger.info(f"  Entry: {entry_price:.2f} | SL: {sl_price:.2f} ({sl_percent:.3f}%) | TP: {tp_price:.2f}")
                logger.info(f"  Risk: ${actual_position_size * sl_distance:.2f} (1% of balance)")
                logger.info(f"  Est. Fees (0.1%): ${total_fees:.2f}")
                return True
            except Exception as e:
                logger.error(f"❌ Failed to execute SHORT: {e}")
        
        return False
    
    def check_and_close_position(self, current_price):
        """Check if position hit SL or TP and close with fee tracking"""
        if not self.active_position:
            return False
        
        position = self.active_position
        
        if position["side"] == "long":
            if current_price <= position["sl"]:
                logger.info(f"\n🛑 STOP LOSS HIT - Long position closed at {current_price:.2f}")
                try:
                    order_id = self.client.place_market_order(SYMBOL, "sell", position["size"])
                    loss = (position["entry"] - current_price) * position["size"]
                    
                    logger.info(f"✅ Position closed: {order_id}")
                    logger.info(f"  Gross Loss: ${loss:.2f}")
                    logger.info(f"  Fees Paid: ${position['fees']:.2f}")
                    logger.info(f"  Net Loss: ${loss + position['fees']:.2f}")
                    logger.info(f"  SL was {position['sl_percent']:.3f}% from entry")
                    
                    self.total_fees_paid += position['fees']
                    self.total_trades += 1
                    self.active_position = None
                    self.position_candle_ts = None
                    return True
                except Exception as e:
                    logger.error(f"❌ Failed to close: {e}")
            
            elif current_price >= position["tp"]:
                logger.info(f"\n🎯 TAKE PROFIT HIT - Long position closed at {current_price:.2f}")
                try:
                    order_id = self.client.place_market_order(SYMBOL, "sell", position["size"])
                    gross_profit = (current_price - position["entry"]) * position["size"]
                    net_profit = gross_profit - position['fees']
                    
                    logger.info(f"✅ Position closed: {order_id}")
                    logger.info(f"  Gross Profit: ${gross_profit:.2f}")
                    logger.info(f"  Fees Paid: ${position['fees']:.2f}")
                    logger.info(f"  Net Profit: ${net_profit:.2f}")
                    logger.info(f"  TP was {position['tp_percent']:.3f}% from entry")
                    
                    self.total_fees_paid += position['fees']
                    self.total_trades += 1
                    self.active_position = None
                    self.position_candle_ts = None
                    return True
                except Exception as e:
                    logger.error(f"❌ Failed to close: {e}")
        
        elif position["side"] == "short":
            if current_price >= position["sl"]:
                logger.info(f"\n🛑 STOP LOSS HIT - Short position closed at {current_price:.2f}")
                try:
                    order_id = self.client.place_market_order(SYMBOL, "buy", position["size"])
                    loss = (current_price - position["entry"]) * position["size"]
                    
                    logger.info(f"✅ Position closed: {order_id}")
                    logger.info(f"  Gross Loss: ${loss:.2f}")
                    logger.info(f"  Fees Paid: ${position['fees']:.2f}")
                    logger.info(f"  Net Loss: ${loss + position['fees']:.2f}")
                    logger.info(f"  SL was {position['sl_percent']:.3f}% from entry")
                    
                    self.total_fees_paid += position['fees']
                    self.total_trades += 1
                    self.active_position = None
                    self.position_candle_ts = None
                    return True
                except Exception as e:
                    logger.error(f"❌ Failed to close: {e}")
            
            elif current_price <= position["tp"]:
                logger.info(f"\n🎯 TAKE PROFIT HIT - Short position closed at {current_price:.2f}")
                try:
                    order_id = self.client.place_market_order(SYMBOL, "buy", position["size"])
                    gross_profit = (position["entry"] - current_price) * position["size"]
                    net_profit = gross_profit - position['fees']
                    
                    logger.info(f"✅ Position closed: {order_id}")
                    logger.info(f"  Gross Profit: ${gross_profit:.2f}")
                    logger.info(f"  Fees Paid: ${position['fees']:.2f}")
                    logger.info(f"  Net Profit: ${net_profit:.2f}")
                    logger.info(f"  TP was {position['tp_percent']:.3f}% from entry")
                    
                    self.total_fees_paid += position['fees']
                    self.total_trades += 1
                    self.active_position = None
                    self.position_candle_ts = None
                    return True
                except Exception as e:
                    logger.error(f"❌ Failed to close: {e}")
        
        return False
    
    def reset_candle_state(self, new_high, new_low):
        """Reset trade tracking for new candle"""
        self.high_entry_used = False
        self.low_entry_used = False
        self.current_candle_high = new_high
        self.current_candle_low = new_low
        
        logger.info("📌 New 4H Candle - Entry flags reset")
        
        if self.active_position:
            logger.info(f"⚠️ Position still active from previous candle")
            logger.info(f"   Position side: {self.active_position['side'].upper()}")
            logger.info(f"   Will continue monitoring until closed")
        else:
            logger.info("✅ Both HIGH and LOW entries available for new candle")
    
    def print_statistics(self):
        """Print trading statistics"""
        logger.info(f"\n{'='*40}")
        logger.info(f"📊 TRADING STATISTICS")
        logger.info(f"  Total Trades Executed: {self.total_trades}")
        logger.info(f"  Trades Rejected (tight SL): {self.rejected_trades}")
        logger.info(f"  Total Fees Paid: ${self.total_fees_paid:.2f}")
        if self.total_trades > 0:
            logger.info(f"  Average Fee per Trade: ${self.total_fees_paid/self.total_trades:.2f}")
        logger.info(f"{'='*40}")
    
    def run(self):
        logger.info("="*60)
        logger.info("💰 XAU-USDT-SWAP PERPETUAL BOT")
        logger.info(f"Risk per trade: {RISK_PERCENT*100}%")
        logger.info(f"Minimum SL Required: {MIN_SL_PERCENT*100}%")
        logger.info(f"Trading Fee: {TRADING_FEE*100}% per transaction (0.1% round trip)")
        logger.info(f"Rules:")
        logger.info(f"  - Only ONE trade per entry per candle")
        logger.info(f"  - HIGH breach → LONG (max 1x per candle)")
        logger.info(f"  - LOW breach → SHORT (max 1x per candle)")
        logger.info(f"  - Trade rejected if SL < {MIN_SL_PERCENT*100}% (too tight)")
        logger.info(f"Lot size: {self.lot_size}")
        logger.info("="*60)
        
        stats_print_time = time.time()
        
        while True:
            try:
                account_balance = self.client.get_account_balance()
                current_price = self.client.get_ticker(SYMBOL)
                self.last_balance = account_balance  # Store for position sizing
                
                logger.info(f"\n💰 Balance: ${account_balance:.2f} | Price: ${current_price:.2f}")
                
                # Print stats every hour
                if time.time() - stats_print_time > 3600:
                    self.print_statistics()
                    stats_print_time = time.time()
                
                candle = self.fetch_latest_4h_candle()
                if not candle:
                    time.sleep(5)
                    continue
                
                candle_ts = candle[0]
                new_high = float(candle[2])
                new_low = float(candle[3])
                
                # Check for new candle
                if self.current_candle_ts != candle_ts:
                    rng, sl_distance, tp_distance = self.calculate_risk_params(new_high, new_low)
                    sl_percent = (sl_distance / current_price) * 100
                    
                    logger.info(f"\n{'='*40}")
                    logger.info(f"📊 NEW 4H CANDLE DETECTED")
                    logger.info(f"  Time: {datetime.fromtimestamp(int(candle_ts)/1000)}")
                    logger.info(f"  High: {new_high:.2f} | Low: {new_low:.2f}")
                    logger.info(f"  Range: {rng:.2f}")
                    logger.info(f"  SL Distance: {sl_distance:.2f} ({sl_percent:.3f}%)")
                    logger.info(f"  TP Distance: {tp_distance:.2f} ({(tp_distance/current_price)*100:.3f}%)")
                    logger.info(f"  Min SL Required: {MIN_SL_PERCENT*100}%")
                    
                    if sl_percent < (MIN_SL_PERCENT * 100):
                        logger.info(f"  ⚠️ WARNING: Current SL {sl_percent:.3f}% is below minimum!")
                        logger.info(f"  Trades from this candle may be rejected")
                    
                    logger.info(f"{'='*40}")
                    
                    self.reset_candle_state(new_high, new_low)
                    self.current_candle_ts = candle_ts
                    self.current_position_size = self.calculate_position_size(
                        account_balance, sl_distance, current_price
                    )
                
                # Check for entry signals (only if no active position)
                if not self.active_position:
                    rng, sl_distance, _ = self.calculate_risk_params(self.current_candle_high, self.current_candle_low)
                    position_size = self.calculate_position_size(account_balance, sl_distance, current_price)
                    self.check_and_execute_entries(current_price, position_size)
                else:
                    # Active position exists - monitor for close
                    self.check_and_close_position(current_price)
                
                # Display status
                if self.active_position:
                    logger.info(f"📊 ACTIVE: {self.active_position['side'].upper()} @ {self.active_position['entry']:.2f} | "
                              f"SL: {self.active_position['sl']:.2f} ({self.active_position['sl_percent']:.3f}%) | "
                              f"TP: {self.active_position['tp']:.2f} ({self.active_position['tp_percent']:.3f}%)")
                else:
                    high_status = "❌ USED" if self.high_entry_used else "✅ AVAILABLE"
                    low_status = "❌ USED" if self.low_entry_used else "✅ AVAILABLE"
                    logger.info(f"📌 Entry Status - HIGH: {high_status} | LOW: {low_status}")
                    if self.rejected_trades > 0:
                        logger.info(f"⚠️ Rejected trades this session: {self.rejected_trades}")
                
                time.sleep(1)
                
            except KeyboardInterrupt:
                logger.info("\n🛑 Bot stopped by user")
                self.print_statistics()
                break
            except Exception as e:
                logger.error(f"Error: {e}")
                time.sleep(5)


if __name__ == "__main__":
    try:
        bot = GoldFuturesBot()
        bot.run()
    except KeyboardInterrupt:
        logger.info("Bot shutdown complete")
