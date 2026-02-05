# services/strategy_exit_service.py

"""
Service for handling strategy position exits at market price.
Supports both live trading and analyzer/sandbox modes.
"""

import time
from typing import Tuple, Optional

from database.settings_db import get_analyze_mode
from utils.logging import get_logger

logger = get_logger(__name__)


def execute_market_exit(
    leg_symbol: str,
    leg_exchange: str,
    leg_product: str,
    leg_quantity: int,
    leg_side: str,
    api_key: str,
    max_retries: int = 10,
    retry_interval: float = 2.0
) -> Tuple[bool, Optional[float], str]:
    """
    Execute a market exit order for a strategy leg position.
    
    Places a MARKET order in the opposite direction of the leg position
    and waits for complete execution to retrieve the fill price.
    
    Args:
        leg_symbol: Symbol without exchange prefix
        leg_exchange: Exchange name (e.g., 'NFO', 'NSE')
        leg_product: Product type (e.g., 'MIS', 'NRML', 'CNC')
        leg_quantity: Position quantity to exit
        leg_side: Original position side ('BUY' or 'SELL')
        api_key: OpenAlgo API key for authentication
        max_retries: Maximum number of status check retries
        retry_interval: Seconds between status checks
        
    Returns:
        Tuple of (success, fill_price, message):
        - success: True if order executed successfully
        - fill_price: Average fill price, None if failed
        - message: Success or error message
    """
    try:
        # Determine exit action (opposite of entry)
        exit_action = 'SELL' if leg_side == 'BUY' else 'BUY'
        
        logger.info(
            f"[StrategyExit] Starting market exit - Symbol: {leg_symbol}, "
            f"Exchange: {leg_exchange}, Quantity: {leg_quantity}, "
            f"Original Side: {leg_side}, Exit Action: {exit_action}"
        )
        
        # Check if in analyzer mode
        is_analyzer = get_analyze_mode()
        
        if is_analyzer:
            logger.info("[StrategyExit] Analyzer mode detected - using sandbox execution")
            return _execute_sandbox_market_exit(
                leg_symbol, leg_exchange, leg_product, leg_quantity, exit_action, api_key
            )
        else:
            logger.info("[StrategyExit] Live mode detected - placing actual broker order")
            return _execute_live_market_exit(
                leg_symbol, leg_exchange, leg_product, leg_quantity, exit_action, 
                api_key, max_retries, retry_interval
            )
            
    except Exception as e:
        error_msg = f"Unexpected error executing market exit: {str(e)}"
        logger.exception(f"[StrategyExit] {error_msg}")
        return False, None, error_msg


def _execute_live_market_exit(
    symbol: str,
    exchange: str,
    product: str,
    quantity: int,
    action: str,
    api_key: str,
    max_retries: int,
    retry_interval: float
) -> Tuple[bool, Optional[float], str]:
    """Execute market exit in live trading mode."""
    from services.place_order_service import place_order
    from services.orderstatus_service import get_order_status
    
    # Prepare order data for market exit
    order_data = {
        'apikey': api_key,
        'strategy': 'StrategyExit',
        'symbol': symbol,
        'exchange': exchange,
        'action': action,
        'quantity': str(quantity),
        'price': '0',  # Market order
        'product': product,
        'pricetype': 'MARKET'
    }
    
    logger.info(f"[StrategyExit] Placing market order: {order_data}")
    
    # Place the market order
    success, response, status_code = place_order(order_data, api_key=api_key)
    
    if not success or response.get('status') != 'success':
        error_msg = response.get('message', 'Failed to place market exit order')
        logger.error(f"[StrategyExit] Order placement failed: {error_msg}")
        return False, None, error_msg
    
    order_id = response.get('orderid')
    if not order_id:
        error_msg = "No order ID returned from broker"
        logger.error(f"[StrategyExit] {error_msg}")
        return False, None, error_msg
    
    logger.info(f"[StrategyExit] Market order placed successfully, Order ID: {order_id}")
    
    # Poll for order completion
    for attempt in range(max_retries):
        time.sleep(retry_interval)
        
        logger.debug(f"[StrategyExit] Checking order status (attempt {attempt + 1}/{max_retries})")
        
        status_data = {'orderid': order_id}
        success, status_response, _ = get_order_status(status_data, api_key=api_key)
        
        if not success or status_response.get('status') != 'success':
            logger.warning(f"[StrategyExit] Failed to get order status: {status_response.get('message')}")
            continue
        
        order_data = status_response.get('data', {})
        order_status = order_data.get('order_status', '').lower()
        
        logger.info(f"[StrategyExit] Order status: {order_status}")
        
        if order_status == 'complete':
            # Order executed successfully
            fill_price = float(order_data.get('average_price', 0))
            
            if fill_price <= 0:
                logger.warning(f"[StrategyExit] Invalid fill price: {fill_price}, attempting to retrieve from price field")
                fill_price = float(order_data.get('price', 0))
            
            if fill_price <= 0:
                error_msg = f"Order completed but invalid fill price: {fill_price}"
                logger.error(f"[StrategyExit] {error_msg}")
                return False, None, error_msg
            
            logger.info(f"[StrategyExit] Order executed successfully at price: {fill_price}")
            return True, fill_price, f"Position exited at market price ₹{fill_price:.2f}"
        
        elif order_status in ['rejected', 'cancelled']:
            error_msg = f"Order {order_status}: {order_data.get('order_tag', 'No reason provided')}"
            logger.error(f"[StrategyExit] {error_msg}")
            return False, None, error_msg
    
    # Timeout - order still pending
    error_msg = f"Order execution timeout after {max_retries * retry_interval} seconds"
    logger.error(f"[StrategyExit] {error_msg}")
    return False, None, error_msg


def _execute_sandbox_market_exit(
    symbol: str,
    exchange: str,
    product: str,
    quantity: int,
    action: str,
    api_key: str
) -> Tuple[bool, Optional[float], str]:
    """Execute market exit in analyzer/sandbox mode using current LTP."""
    from services.market_data_service import get_ltp_value
    
    logger.info(f"[StrategyExit] Sandbox mode - fetching LTP for {symbol}")
    
    # Get current LTP from market data service
    ltp = get_ltp_value(symbol, exchange)
    
    if ltp is None or ltp <= 0:
        # Fallback: try to get from quotes service
        from services.quotes_service import get_quotes
        
        logger.warning(f"[StrategyExit] LTP not available from market data, trying quotes service")
        
        quotes_data = {
            'symbol': symbol,
            'exchange': exchange
        }
        
        success, quotes_response, _ = get_quotes(quotes_data, api_key=api_key)
        
        if success and quotes_response.get('status') == 'success':
            ltp = quotes_response.get('data', {}).get('ltp')
            if ltp:
                ltp = float(ltp)
        
        if not ltp or ltp <= 0:
            error_msg = f"Unable to fetch LTP for {symbol} on {exchange}"
            logger.error(f"[StrategyExit] {error_msg}")
            return False, None, error_msg
    
    logger.info(f"[StrategyExit] Sandbox exit using LTP: {ltp}")
    
    # In sandbox mode, we simulate immediate execution at LTP
    # The actual sandbox order will be placed through the normal flow
    from services.place_order_service import place_order
    
    order_data = {
        'apikey': api_key,
        'strategy': 'StrategyExit',
        'symbol': symbol,
        'exchange': exchange,
        'action': action,
        'quantity': str(quantity),
        'price': '0',
        'product': product,
        'pricetype': 'MARKET'
    }
    
    success, response, status_code = place_order(order_data, api_key=api_key)
    
    if not success or response.get('status') not in ('success', 'analyze'):
        error_msg = response.get('message', 'Failed to place sandbox exit order')
        logger.error(f"[StrategyExit] Sandbox order placement failed: {error_msg}")
        return False, None, error_msg
    
    logger.info(f"[StrategyExit] Sandbox order placed successfully, using LTP {ltp} as fill price")
    return True, ltp, f"Position exited at market price ₹{ltp:.2f} (Analyzer mode)"
