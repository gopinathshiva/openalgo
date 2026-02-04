# blueprints/strategy_state.py

"""
Blueprint for Strategy State API endpoints.
Provides read-only access to Python strategy execution states and positions.
"""

from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session

from database.strategy_state_db import (
    StrategyStateDbError,
    StrategyStateDbNotFoundError,
    StrategyStateDuplicateLegError,
    StrategyStateNotFoundError,
    add_manual_strategy_leg,
    create_strategy_override,
    delete_strategy_state,
    get_all_strategy_states,
    get_strategy_state_by_instance_id,
)
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

strategy_state_bp = Blueprint('strategy_state_bp', __name__, url_prefix='/api')


OPEN_LEG_STATUSES = {'IN_POSITION', 'PENDING_ENTRY', 'PENDING_EXIT'}


def compute_strategy_state_summary(state: dict) -> dict:
    """Compute summary metrics for a strategy state.

    P&L aggregation rules (to avoid double-counting):
      - Realized P&L is derived exclusively from `trade_history`.
      - Unrealized P&L is derived exclusively from legs that are currently open.

    Args:
        state: Strategy state dictionary as returned by `database.strategy_state_db`.

    Returns:
        Summary dictionary.
    """

    legs = state.get('legs', {}) or {}
    trade_history = state.get('trade_history', []) or []

    total_unrealized_pnl = 0.0
    open_positions_count = 0
    idle_positions_count = 0

    for _, leg in legs.items():
        leg_status = (leg or {}).get('status', '')

        if leg_status in OPEN_LEG_STATUSES:
            open_positions_count += 1
            total_unrealized_pnl += float((leg or {}).get('unrealized_pnl', 0) or 0)
        elif leg_status == 'IDLE':
            idle_positions_count += 1

    total_realized_pnl = sum(float((t or {}).get('pnl', 0) or 0) for t in trade_history)

    return {
        'total_realized_pnl': total_realized_pnl,
        'total_unrealized_pnl': total_unrealized_pnl,
        'total_pnl': total_realized_pnl + total_unrealized_pnl,
        # Backwards-compatible field name; now it equals realized P&L by design.
        'trade_history_pnl': total_realized_pnl,
        'open_positions_count': open_positions_count,
        'idle_positions_count': idle_positions_count,
        'total_trades': len(trade_history),
    }


@strategy_state_bp.route('/strategy-state', methods=['GET'])
@check_session_validity
def get_strategy_states():
    """
    Get all strategy execution states with positions and trade history.

    Returns:
        JSON response with list of strategy states
    """
    try:
        logger.debug("GET /api/strategy-state called")
        states = get_all_strategy_states()
        logger.debug(f"Found {len(states)} strategy states")

        # Calculate summary statistics for each strategy
        # Summary counts must match the Strategy Positions UI logic.
        for state in states:
            state['summary'] = compute_strategy_state_summary(state)

        return jsonify({
            'status': 'success',
            'data': states
        })

    except Exception as e:
        logger.error(f"Error in get_strategy_states: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@strategy_state_bp.route('/strategy-state/<path:instance_id>', methods=['GET'])
@check_session_validity
def get_strategy_state(instance_id):
    """
    Get a specific strategy state by instance_id.

    Args:
        instance_id: The unique instance identifier

    Returns:
        JSON response with strategy state
    """
    try:
        state = get_strategy_state_by_instance_id(instance_id)

        if not state:
            return jsonify({
                'status': 'error',
                'message': f'Strategy state not found: {instance_id}'
            }), 404

        # Keep response consistent with list endpoint by including computed summary.
        state['summary'] = compute_strategy_state_summary(state)

        return jsonify({
            'status': 'success',
            'data': state
        })

    except Exception as e:
        logger.error(f"Error in get_strategy_state: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@strategy_state_bp.route('/strategy-state/<path:instance_id>', methods=['DELETE'])
@check_session_validity
def delete_strategy_state_endpoint(instance_id):
    """
    Delete a specific strategy state by instance_id.

    Args:
        instance_id: The unique instance identifier

    Returns:
        JSON response with deletion status
    """
    try:
        logger.debug(f"DELETE request for instance_id: {instance_id}")

        try:
            delete_strategy_state(instance_id)
        except StrategyStateNotFoundError:
            # This should not happen since we already verified existence above
            logger.warning(f"Strategy state not found during delete: {instance_id}")
            return jsonify({
                'status': 'error',
                'message': f'Strategy state not found: {instance_id}'
            }), 404
        except StrategyStateDbNotFoundError as e:
            logger.error(f"Strategy State DB missing: {e}")
            return jsonify({
                'status': 'error',
                'message': str(e)
            }), 500
        except StrategyStateDbError as e:
            logger.error(f"Strategy State DB error deleting {instance_id}: {e}")
            return jsonify({
                'status': 'error',
                'message': str(e)
            }), 500

        logger.debug(f"Strategy state deleted successfully: {instance_id}")
        return jsonify({
            'status': 'success',
            'message': f'Strategy state deleted: {instance_id}'
        })

    except Exception as e:
        logger.error(f"Error in delete_strategy_state: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@strategy_state_bp.route('/strategy-state/<path:instance_id>/manual-leg', methods=['POST'])
@check_session_validity
def create_manual_leg_endpoint(instance_id):
    """Add a manual open position leg to the strategy state.

    Request body:
        {
            "leg_key": "MANUAL_1",
            "symbol": "SENSEX05FEB2682200CE",
            "exchange": "BFO",
            "product": "NRML",
            "quantity": 200,
            "side": "SELL",
            "entry_price": 733.05,
            "sl_percent": 0.05,
            "target_percent": 0.1,
            "leg_pair_name": "Manual Hedge",
            "is_main_leg": false
        }
    """
    try:
        logger.info(f"POST /api/strategy-state/{instance_id}/manual-leg called")

        data = request.get_json()
        logger.debug(f"Request data: {data}")

        if not data:
            return jsonify({
                'status': 'error',
                'message': 'Request body is required'
            }), 400

        leg_key = data.get('leg_key')
        symbol = data.get('symbol')
        exchange = data.get('exchange')
        product = data.get('product')
        quantity = data.get('quantity')
        side = data.get('side')
        entry_price = data.get('entry_price')
        sl_percent = data.get('sl_percent')
        target_percent = data.get('target_percent')
        leg_pair_name = data.get('leg_pair_name')
        is_main_leg = data.get('is_main_leg')
        reentry_limit = data.get('reentry_limit')
        reexecute_limit = data.get('reexecute_limit')
        mode = data.get('mode', 'TRACK')  # TRACK or NEW
        wait_trade_percent = data.get('wait_trade_percent')
        wait_baseline_price = data.get('wait_baseline_price')

        # Validate mode
        if mode not in ('TRACK', 'NEW'):
            return jsonify({'status': 'error', 'message': 'mode must be TRACK or NEW'}), 400

        required_fields = {
            'leg_key': 'leg_key is required',
            'symbol': 'symbol is required',
            'exchange': 'exchange is required',
            'product': 'product is required',
            'quantity': 'quantity is required',
            'side': 'side is required',
            'is_main_leg': 'is_main_leg is required',
        }

        for field, message in required_fields.items():
            # Note: `is_main_leg` can be `False`, so check for `is None`
            if data.get(field) is None:
                return jsonify({'status': 'error', 'message': message}), 400

        if mode == 'NEW':
            # For new trades, entry_price is optional (will be determined on fill)
            if entry_price is not None:
                try:
                    entry_price = float(entry_price)
                except (TypeError, ValueError):
                    return jsonify({'status': 'error', 'message': 'entry_price must be a number'}), 400
        else:
            # For tracking existing trades, entry_price is required
            if entry_price is None:
                return jsonify({'status': 'error', 'message': 'entry_price is required'}), 400
            try:
                entry_price = float(entry_price)
            except (TypeError, ValueError):
                return jsonify({'status': 'error', 'message': 'entry_price must be a number'}), 400

        side = side.upper()
        if side not in ('BUY', 'SELL'):
            return jsonify({'status': 'error', 'message': 'side must be BUY or SELL'}), 400

        # Determine status and wait params
        status = 'IN_POSITION'
        if mode == 'NEW':
            status = 'PENDING_ENTRY'
            if wait_trade_percent is not None:
                try:
                    wait_trade_percent = float(wait_trade_percent)
                    if wait_trade_percent <= 0:
                         return jsonify({'status': 'error', 'message': 'wait_trade_percent must be positive'}), 400

                    if wait_baseline_price is None:
                        return jsonify({'status': 'error', 'message': 'wait_baseline_price is required for wait entry'}), 400
                    wait_baseline_price = float(wait_baseline_price)
                except (TypeError, ValueError):
                     return jsonify({'status': 'error', 'message': 'Invalid wait parameters'}), 400
            else:
                 # Immediate execution (wait_trade_percent is None)
                 pass

        sl_value = None
        target_value = None

        # Calculate SL/Target values only if entry_price is available
        if entry_price is not None:
            if sl_percent is not None:
                try:
                    sl_percent = float(sl_percent)
                except (TypeError, ValueError):
                    return jsonify({'status': 'error', 'message': 'sl_percent must be a number'}), 400
                if sl_percent <= 0:
                    return jsonify({'status': 'error', 'message': 'sl_percent must be positive'}), 400
                if side == 'BUY':
                    sl_value = entry_price * (1 - sl_percent)
                else:
                    sl_value = entry_price * (1 + sl_percent)
        elif sl_percent is not None:
             # Validate percent even if no entry price
             try:
                 sl_percent = float(sl_percent)
                 if sl_percent <= 0: raise ValueError
             except (TypeError, ValueError):
                 return jsonify({'status': 'error', 'message': 'sl_percent must be positive'}), 400

        if target_percent is not None and entry_price is not None:
            try:
                target_percent = float(target_percent)
            except (TypeError, ValueError):
                return jsonify({'status': 'error', 'message': 'target_percent must be a number'}), 400
            if target_percent <= 0:
                return jsonify({'status': 'error', 'message': 'target_percent must be positive'}), 400
            if side == 'BUY':
                target_value = entry_price * (1 + target_percent)
            else:
                target_value = entry_price * (1 - target_percent)
        elif target_percent is not None:
             try:
                 target_percent = float(target_percent)
                 if target_percent <= 0: raise ValueError
             except (TypeError, ValueError):
                 return jsonify({'status': 'error', 'message': 'target_percent must be positive'}), 400

        # Validate reentry_limit and reexecute_limit if provided
        if reentry_limit is not None:
            try:
                reentry_limit = int(reentry_limit)
            except (TypeError, ValueError):
                return jsonify({'status': 'error', 'message': 'reentry_limit must be an integer'}), 400
            if reentry_limit < 0:
                return jsonify({'status': 'error', 'message': 'reentry_limit must be non-negative'}), 400

        if reexecute_limit is not None:
            try:
                reexecute_limit = int(reexecute_limit)
            except (TypeError, ValueError):
                return jsonify({'status': 'error', 'message': 'reexecute_limit must be an integer'}), 400
            if reexecute_limit < 0:
                return jsonify({'status': 'error', 'message': 'reexecute_limit must be non-negative'}), 400

        try:
            logger.info(f"Calling add_manual_strategy_leg for instance {instance_id}, leg_key {leg_key}")
            result = add_manual_strategy_leg(
                instance_id=instance_id,
                leg_key=leg_key,
                symbol=symbol,
                exchange=exchange,
                product=product,
                quantity=quantity,
                side=side,
                entry_price=entry_price,
                entry_time=datetime.now(timezone.utc),
                sl_price=sl_value,
                target_price=target_value,
                leg_pair_name=leg_pair_name,
                is_main_leg=bool(is_main_leg),
                sl_percent=sl_percent,
                target_percent=target_percent,
                reentry_limit=reentry_limit,
                reexecute_limit=reexecute_limit,
                status=status,
                wait_trade_percent=wait_trade_percent,
                wait_baseline_price=wait_baseline_price,
            )
            logger.info(f"Manual leg added successfully to {instance_id}")
        except StrategyStateDuplicateLegError as exc:
            logger.warning(f"Duplicate leg detected: {exc}")
            return jsonify({'status': 'error', 'message': str(exc)}), 409
        except StrategyStateNotFoundError:
            return jsonify({'status': 'error', 'message': f'Strategy state not found: {instance_id}'}), 404
        except StrategyStateDbNotFoundError as exc:
            logger.error(f"Strategy State DB missing: {exc}")
            return jsonify({'status': 'error', 'message': str(exc)}), 500
        except StrategyStateDbError as exc:
            logger.error(f"Strategy State DB error adding manual leg: {exc}")
            return jsonify({'status': 'error', 'message': str(exc)}), 500

        return jsonify({
            'status': 'success',
            'message': 'Manual leg added successfully',
            'data': result,
        })

    except Exception as exc:
        logger.error(f"Error in create_manual_leg_endpoint: {exc}")
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@strategy_state_bp.route('/strategy-state/<path:instance_id>/leg/<path:leg_key>/manual-exit', methods=['POST'])
@check_session_validity
def manual_exit_leg_endpoint(instance_id, leg_key):
    """
    Manually exit a leg position and mark it with SL_HIT or TARGET_HIT status.

    Args:
        instance_id: The unique instance identifier
        leg_key: The leg key to exit

    Request body:
        {
            "exit_price": 123.45,  # Optional if exit_at_market=true
            "exit_status": "SL_HIT" | "TARGET_HIT",
            "exit_at_market": false  # Optional, default false
        }

    Returns:
        JSON response with updated strategy state or error
    """
    try:
        logger.info(f"POST /api/strategy-state/{instance_id}/leg/{leg_key}/manual-exit called")

        data = request.get_json()

        if not data:
            return jsonify({
                'status': 'error',
                'message': 'Request body is required'
            }), 400

        exit_price = data.get('exit_price')
        exit_status = data.get('exit_status')
        exit_at_market = data.get('exit_at_market', False)

        # For market exits, exit_status is optional and defaults to MANUAL_EXIT
        if exit_at_market:
            if not exit_status:
                exit_status = 'MANUAL_EXIT'
            elif exit_status not in ('SL_HIT', 'TARGET_HIT', 'MANUAL_EXIT'):
                return jsonify({
                    'status': 'error',
                    'message': 'exit_status must be SL_HIT, TARGET_HIT, or MANUAL_EXIT'
                }), 400
        else:
            # For manual price exits, exit_status is required
            if not exit_status:
                return jsonify({
                    'status': 'error',
                    'message': 'exit_status is required for manual price exits'
                }), 400

            if exit_status not in ('SL_HIT', 'TARGET_HIT'):
                return jsonify({
                    'status': 'error',
                    'message': 'exit_status must be SL_HIT or TARGET_HIT for manual price exits'
                }), 400

        # Validate exit_price based on exit_at_market flag
        if exit_at_market:
            # Market exit - price will be determined by order execution
            logger.info(f"Market exit requested for {instance_id}/{leg_key}")
            exit_price = None  # Will be filled after order execution
        else:
            # Manual price exit - price is required
            if exit_price is None:
                return jsonify({
                    'status': 'error',
                    'message': 'exit_price is required when exit_at_market is false'
                }), 400

            # Validate exit_price is a number
            try:
                exit_price = float(exit_price)
            except (TypeError, ValueError):
                return jsonify({
                    'status': 'error',
                    'message': 'exit_price must be a valid number'
                }), 400

            if exit_price <= 0:
                return jsonify({
                    'status': 'error',
                    'message': 'exit_price must be positive'
                }), 400

        # Get the strategy state
        state = get_strategy_state_by_instance_id(instance_id)
        if not state:
            return jsonify({
                'status': 'error',
                'message': f'Strategy state not found: {instance_id}'
            }), 404

        legs = state.get('legs', {})
        if leg_key not in legs:
            return jsonify({
                'status': 'error',
                'message': f'Leg not found: {leg_key}'
            }), 404

        leg = legs[leg_key]

        # Verify the leg is in ACTIVE status
        if leg.get('status') != 'IN_POSITION':
            return jsonify({
                'status': 'error',
                'message': f'Can only exit legs with IN_POSITION status. Current status: {leg.get("status")}'
            }), 400
        
        # Track leg type and strategy status for override creation
        leg_type = leg.get('leg_type', 'MANUAL')
        strategy_status = state.get('status', 'COMPLETED')

        # If market exit is requested, execute the order first
        if exit_at_market:
            from services.strategy_exit_service import execute_market_exit
            from database.auth_db import get_api_key_for_tradingview
            
            # Get username from session
            username = session.get('user')
            if not username:
                return jsonify({
                    'status': 'error',
                    'message': 'User not found in session'
                }), 401
            
            # Get API key for the user
            api_key = get_api_key_for_tradingview(username)
            if not api_key:
                return jsonify({
                    'status': 'error',
                    'message': 'API key not found for user'
                }), 401
            
            # Extract leg details
            leg_symbol = leg.get('symbol')
            leg_exchange = leg.get('exchange')
            leg_product = leg.get('product')
            leg_quantity = leg.get('quantity')
            leg_side = leg.get('side')
            
            # If product not in leg, try to get from strategy config
            if not leg_product:
                config = state.get('config', {})
                leg_product = config.get('product')
                if leg_product:
                    logger.info(f"[MarketExit] Product not in leg, using from config: {leg_product}")
                else:
                    logger.warning(f"[MarketExit] Product not found in leg or config")
            
            # Debug logging to see what's in the leg
            logger.info(f"[MarketExit] Leg data: symbol={leg_symbol}, exchange={leg_exchange}, product={leg_product}, quantity={leg_quantity}, side={leg_side}")
            logger.info(f"[MarketExit] Full leg dict keys: {list(leg.keys())}")
            logger.info(f"[MarketExit] Full leg dict: {leg}")
            
            # Check for required fields
            if not leg_symbol:
                return jsonify({
                    'status': 'error',
                    'message': 'Symbol is required for market exit'
                }), 400
            
            if not leg_quantity or leg_quantity <= 0:
                return jsonify({
                    'status': 'error',
                    'message': 'Quantity is required for market exit'
                }), 400
            
            if not leg_side:
                return jsonify({
                    'status': 'error',
                    'message': 'Side (BUY/SELL) is required for market exit'
                }), 400
            
            # For exchange and product, try to infer from symbol or use defaults
            if not leg_exchange:
                # Try to infer from symbol format
                symbol_upper = leg_symbol.upper()
                
                # Check for options (CE/PE suffix indicates derivatives)
                if 'CE' in symbol_upper or 'PE' in symbol_upper:
                    # Options - determine exchange based on underlying
                    if 'SENSEX' in symbol_upper or 'BANKEX' in symbol_upper:
                        leg_exchange = 'BFO'  # BSE derivatives
                        logger.warning(f"[MarketExit] Exchange not found, inferred BFO from symbol: {leg_symbol}")
                    elif 'NIFTY' in symbol_upper or 'BANKNIFTY' in symbol_upper or 'FINNIFTY' in symbol_upper or 'MIDCPNIFTY' in symbol_upper:
                        leg_exchange = 'NFO'  # NSE derivatives
                        logger.warning(f"[MarketExit] Exchange not found, inferred NFO from symbol: {leg_symbol}")
                    else:
                        # Unknown options - default to NFO
                        leg_exchange = 'NFO'
                        logger.warning(f"[MarketExit] Exchange not found, defaulting to NFO for options symbol: {leg_symbol}")
                else:
                    # Equity - default to NSE
                    leg_exchange = 'NSE'
                    logger.warning(f"[MarketExit] Exchange not found, inferred NSE for equity symbol: {leg_symbol}")
            
            if not leg_product:
                # Default to MIS for intraday
                leg_product = 'MIS'
                logger.warning(f"[MarketExit] Product not found, defaulting to MIS for {leg_symbol}")
            
            logger.info(f"Executing market exit for {instance_id}/{leg_key}")
            
            # Execute market exit order
            success, fill_price, message = execute_market_exit(
                leg_symbol=leg_symbol,
                leg_exchange=leg_exchange,
                leg_product=leg_product,
                leg_quantity=leg_quantity,
                leg_side=leg_side,
                api_key=api_key
            )
            
            if not success:
                logger.error(f"Market exit failed: {message}")
                return jsonify({
                    'status': 'error',
                    'message': f'Market exit failed: {message}'
                }), 400
            
            # Use the fill price from order execution
            exit_price = fill_price
            logger.info(f"Market exit successful at price {exit_price}")
        
        # Validate exit price based on side and exit_status (only for manual exit)
        if not exit_at_market:
            entry_price = leg.get('entry_price')
            side = leg.get('side')

            if entry_price and side:
                if exit_status == 'TARGET_HIT':
                    if side == 'BUY' and exit_price <= entry_price:
                        return jsonify({
                            'status': 'error',
                            'message': f'For BUY positions with TARGET_HIT, exit price must be greater than entry price ({entry_price})'
                        }), 400
                    elif side == 'SELL' and exit_price >= entry_price:
                        return jsonify({
                            'status': 'error',
                            'message': f'For SELL positions with TARGET_HIT, exit price must be less than entry price ({entry_price})'
                        }), 400

                elif exit_status == 'SL_HIT':
                    if side == 'BUY' and exit_price >= entry_price:
                        return jsonify({
                            'status': 'error',
                            'message': f'For BUY positions with SL_HIT, exit price must be less than entry price ({entry_price})'
                        }), 400
                    elif side == 'SELL' and exit_price <= entry_price:
                        return jsonify({
                            'status': 'error',
                            'message': f'For SELL positions with SL_HIT, exit price must be greater than entry price ({entry_price})'
                        }), 400

        # Update the leg in database
        from database.strategy_state_db import manual_exit_strategy_leg, create_strategy_override
        try:
            result = manual_exit_strategy_leg(
                instance_id=instance_id,
                leg_key=leg_key,
                exit_price=exit_price,
                exit_status=exit_status,
                exit_time=datetime.now(timezone.utc)
            )

            logger.info(f"Manually exited leg {leg_key} in strategy {instance_id} with status {exit_status}")
            
            # If this is a running Python strategy, create a MANUAL_EXIT override
            # so the strategy can detect and respect the manual exit
            if leg_type != 'MANUAL' and strategy_status == 'RUNNING':
                try:
                    create_strategy_override(
                        instance_id=instance_id,
                        leg_key=leg_key,
                        override_type='MANUAL_EXIT',
                        new_value=exit_price
                    )
                    logger.info(f"Created MANUAL_EXIT override for {instance_id}/{leg_key}")
                except Exception as override_error:
                    logger.warning(f"Failed to create MANUAL_EXIT override: {override_error}")

            return jsonify({
                'status': 'success',
                'message': f'Position exited successfully with status {exit_status}',
                'data': result
            })

        except StrategyStateNotFoundError:
            return jsonify({'status': 'error', 'message': f'Strategy state not found: {instance_id}'}), 404
        except StrategyStateDbNotFoundError as exc:
            logger.error(f"Strategy State DB missing: {exc}")
            return jsonify({'status': 'error', 'message': str(exc)}), 500
        except StrategyStateDbError as exc:
            logger.error(f"Strategy State DB error during manual exit: {exc}")
            return jsonify({'status': 'error', 'message': str(exc)}), 500

    except Exception as exc:
        logger.error(f"Error in manual_exit_leg_endpoint: {exc}")
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@strategy_state_bp.route('/strategy-state/<path:instance_id>/override', methods=['POST'])
@check_session_validity
def create_strategy_override_endpoint(instance_id):
    """
    Create a strategy override for SL or Target price modification.
    The running strategy will poll for and apply these overrides.

    Args:
        instance_id: The unique instance identifier

    Request body:
        {
            "leg_key": "CE_SPREAD_CE_SELL",
            "override_type": "sl_price" | "target_price",
            "new_value": 123.45
        }

    Returns:
        JSON response with created override or error
    """
    try:
        data = request.get_json()

        if not data:
            return jsonify({
                'status': 'error',
                'message': 'Request body is required'
            }), 400

        leg_key = data.get('leg_key')
        override_type = data.get('override_type')
        new_value = data.get('new_value')

        # Validate required fields
        if not leg_key:
            return jsonify({
                'status': 'error',
                'message': 'leg_key is required'
            }), 400

        if not override_type:
            return jsonify({
                'status': 'error',
                'message': 'override_type is required'
            }), 400

        if override_type not in ('sl_price', 'target_price'):
            return jsonify({
                'status': 'error',
                'message': 'override_type must be sl_price or target_price'
            }), 400

        if new_value is None:
            return jsonify({
                'status': 'error',
                'message': 'new_value is required'
            }), 400

        try:
            new_value = float(new_value)
        except (TypeError, ValueError):
            return jsonify({
                'status': 'error',
                'message': 'new_value must be a valid number'
            }), 400

        if new_value < 0:
            return jsonify({
                'status': 'error',
                'message': 'new_value must be non-negative'
            }), 400

        # Verify the leg exists in the strategy and is in position
        state = get_strategy_state_by_instance_id(instance_id)
        if not state:
            return jsonify({
                'status': 'error',
                'message': f'Strategy state not found: {instance_id}'
            }), 404

        legs = state.get('legs', {})
        if leg_key not in legs:
            return jsonify({
                'status': 'error',
                'message': f'Leg not found: {leg_key}'
            }), 404

        # Verify the leg is in a position (can only modify active positions)
        leg = legs[leg_key]
        if leg.get('status') != 'IN_POSITION':
            return jsonify({
                'status': 'error',
                'message': f'Can only modify SL/Target for legs in IN_POSITION status. Current status: {leg.get("status")}'
            }), 400

        # Create the override
        try:
            result = create_strategy_override(
                instance_id=instance_id,
                leg_key=leg_key,
                override_type=override_type,
                new_value=new_value,
            )
        except StrategyStateDbNotFoundError as e:
            # Server-side issue (DB missing)
            logger.error(f"Strategy State DB missing: {e}")
            return jsonify({
                'status': 'error',
                'message': str(e)
            }), 500
        except StrategyStateDbError as e:
            logger.error(f"Strategy State DB error creating override: {e}")
            return jsonify({
                'status': 'error',
                'message': str(e)
            }), 500

        logger.info(f"Created override for {instance_id}/{leg_key}: {override_type}={new_value}")

        return jsonify({
            'status': 'success',
            'message': f'{override_type.replace("_", " ").title()} override created. Will be applied within 5 seconds.',
            'data': result
        })

    except Exception as e:
        logger.error(f"Error in create_strategy_override: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500
