import eth_typing
from web3 import Web3
from web3.exceptions import ContractLogicError
import json
import os  # For file existence check
from dotenv import load_dotenv
# --- CONSTANTS ---
# Ethereum network connection parameters
load_dotenv()

RPC_URL = os.getenv("INFURA_ETH_MAINNET_RPC", "https://eth.llamarpc.com")

# Aave V3 contract addresses on Ethereum Mainnet
# UiPoolDataProviderV3 - provides aggregated pool data
UIPOOL_DATA_PROVIDER_ADDRESS_RAW = "0x3F78BBD206e4D3c504Eb854232EdA7e47E9Fd8FC"
UIPOOL_DATA_PROVIDER_ADDRESS = Web3.to_checksum_address(UIPOOL_DATA_PROVIDER_ADDRESS_RAW)

# PoolAddressesProvider - provides addresses of other key Aave contracts
POOL_ADDRESSES_PROVIDER_ADDRESS_RAW = "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e"
POOL_ADDRESSES_PROVIDER_ADDRESS = Web3.to_checksum_address(POOL_ADDRESSES_PROVIDER_ADDRESS_RAW)

# Path to the ABI file & accessible here https://etherscan.io/address/0x3F78BBD206e4D3c504Eb854232EdA7e47E9Fd8FC#code
UIPOOL_ABI_PATH = os.getenv("UIPOOL_ABI_PATH", "testabi.json")


# --- HELPER FUNCTIONS ---

def connect_to_web3(rpc_url: str) -> Web3:
    """Establishes a connection to the Ethereum network via an RPC URL."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise ConnectionError(f"Failed to connect to Ethereum network at: {rpc_url}. Please check RPC_URL.")
    return w3


def load_abi(abi_path: str) -> list:
    """Loads a contract ABI from a JSON file."""
    if not os.path.exists(abi_path):
        raise FileNotFoundError(f"ABI file not found: '{abi_path}'. Please provide the correct path.")
    try:
        with open(abi_path, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        raise ValueError(f"Invalid JSON format in ABI file: '{abi_path}'. Please ensure the file is correct.")


def get_aave_reserves_data(web3_instance: Web3, data_provider_address: eth_typing.ChecksumAddress, provider_abi: list,
                           pool_addresses_provider: eth_typing.ChecksumAddress) -> tuple:
    """
    Calls the getReservesData function on the UiPoolDataProviderV3 contract.
    Returns reserve data and market base currency information.
    """
    try:
        contract = web3_instance.eth.contract(address=data_provider_address, abi=provider_abi)
        # Set a high gas limit for read-only calls, if low limit is provided, execution will be reverted
        gas_limit = 30_000_000
        result = contract.functions.getReservesData(pool_addresses_provider).call(
            {'gas': gas_limit})
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise ValueError(
                f"Unexpected response format from getReservesData. Expected (list, tuple) with 2 elements, got: {type(result)}, length: {len(result) if isinstance(result, (list, tuple)) else 'N/A'}")
        return result[0], result[1]  # reserves_data, base_currency_info
    except ContractLogicError as e:
        raise ContractLogicError(f"Contract logic error when calling getReservesData: {e}")
    except Exception as e:
        raise Exception(f"Unknown error while fetching reserve data: {e}")


def calculate_total_tvl(reserves_data: list, base_currency_info: list, ) -> float:
    """
    Calculates the total TVL (Total Available) based on reserve data.
    """
    total_tvl_in_usd = 0.0
    total_liquidity_in_usd = 0.0

    # Decode BaseCurrencyInfo (for price conversion)
    market_ref_currency_unit = base_currency_info[0]
    market_ref_currency_price_in_usd_raw = base_currency_info[1]
    price_decimals = base_currency_info[3]

    if price_decimals > 0:
        market_ref_currency_price_in_usd = market_ref_currency_price_in_usd_raw / (10 ** price_decimals)
    else:
        market_ref_currency_price_in_usd = float(market_ref_currency_price_in_usd_raw)

    print(f"\n--- Calculating Total Value Locked (TVL) ---")
    print(f"Market Reference Currency (for calculation): ${market_ref_currency_price_in_usd:.4f} per unit")
    print(f"Market Reference Currency Unit: {market_ref_currency_unit}\n")
    print(f"Processing {len(reserves_data)} reserves for TVL...")

    for i, reserve in enumerate(reserves_data):
        try:
            # AggregatedReserveData fields necessary for TVL calculation:
            symbol = reserve[2]  # Token symbol
            asset_decimals = reserve[3]  # Decimals of the underlying asset
            is_active = reserve[10]  # Whether the reserve is active
            is_frozen = reserve[11]  # Whether the reserve is frozen

            available_liquidity_raw = reserve[20]  # Available liquidity
            total_scaled_variable_debt_raw = reserve[21]  # Total variable debt

            price_in_market_ref_currency = reserve[22]  # Price of the asset in market reference currency units

            # Skip inactive or frozen reserves as they don't contribute to active TVL
            if not is_active or is_frozen:
                # print(f"  Reserve {i+1} ({symbol}) is inactive or frozen. Skipping.")
                continue

            # Total amount of the asset "supplied" to the protocol for this reserve.
            # This is the sum of available liquidity and borrowed variable debt.
            # This metric corresponds to Aave's "Total Available"
            total_supplied_asset_raw = available_liquidity_raw #+ total_scaled_variable_debt_raw
                                                               # Uncomment if you want Market Size instead
            # Normalize the total supplied amount by the asset's decimals
            normalized_total_supplied_asset = total_supplied_asset_raw / (10 ** asset_decimals)
            # Calculate the value of this reserve in market reference currency units
            if market_ref_currency_unit == 0:
                print(f"  Error: Market Reference Currency Unit is 0. Cannot calculate for {symbol}. Skipping.")
                continue

            value_in_market_ref_currency = ((normalized_total_supplied_asset * price_in_market_ref_currency)
                                            / market_ref_currency_unit)

            # Convert the value to USD
            value_in_usd = value_in_market_ref_currency * market_ref_currency_price_in_usd

            total_tvl_in_usd += value_in_usd

            print(
                f"  Reserve {i + 1} ({symbol}): Total Locked: {normalized_total_supplied_asset:,.4f}, Value in USD: ${value_in_usd:,.2f}")

        except IndexError as ie:
            print(f"  IndexError accessing reserve fields for reserve {i + 1}: {ie}. Skipping.")
        except Exception as ex:
            print(f"  Unknown error processing reserve {i + 1}: {ex}. Skipping.")
    return total_tvl_in_usd


# --- MAIN SCRIPT LOGIC ---
def main():
    try:
        # 1. Connect to Web3
        w3 = connect_to_web3(RPC_URL)
        print(f"Latest block from RPC: {w3.eth.block_number}")

        # 2. Load ABI
        uipool_abi = load_abi(UIPOOL_ABI_PATH)

        # 3. Get Aave reserve data
        print(f"\nCalling 'getReservesData' function for address: {POOL_ADDRESSES_PROVIDER_ADDRESS}...")
        reserves_data, base_currency_info = get_aave_reserves_data(
            w3,
            UIPOOL_DATA_PROVIDER_ADDRESS,
            uipool_abi,
            POOL_ADDRESSES_PROVIDER_ADDRESS
        )

        # 4. Calculate TVL
        tvl_result = calculate_total_tvl(reserves_data, base_currency_info)

        print(f"\n--- TVL Calculation Complete ---")
        print(f"Total Value Locked (TVL) in USD: **${tvl_result:,.2f}**")

    except (ConnectionError, FileNotFoundError, ValueError, ContractLogicError) as e:
        print(f"\nERROR: {e}")
    except Exception as e:
        print(f"\nUnknown critical error: {e}")


if __name__ == "__main__":
    main()