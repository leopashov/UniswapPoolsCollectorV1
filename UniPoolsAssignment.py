import os
from typing import Iterable
from dotenv import load_dotenv
from web3 import Web3
import json
import math
import requests
import pandas as pd
from pycoingecko import CoinGeckoAPI

class Contract:
    """ contract class, has main web3/ infura setup, environment variables etc"""
    def __init__(self):
        # print("initiliasing contract")
        load_dotenv()
        self.etherscan_token = os.getenv('ETHERSCAN_TOKEN')
        self.provider_url = "https://mainnet.infura.io/v3/d758f6f480b64b8daf47412f0969392b"
        self.w3 = Web3(Web3.HTTPProvider(self.provider_url))
        self.etherscan_url = "https://api.etherscan.io/api?module=contract&action=getabi&address={}&apikey={}"
        self.s = requests.Session()
        # print("contract initialised")

    def getAbi(self, address):
        address = self.getImplementationContractIfExists(address)
        url = self.etherscan_url.format(address, self.etherscan_token)
        r = self.s.get(url)
        if address == '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48':
            f = open("./USDC_ABI.json")
            abi = json.load(f)
        else:
            if r.status_code == 200:
                abi = r.json()["result"]
            else:
                raise Exception(f"API request status fault, response: ", r.json())
        return abi

    def getImplementationContractIfExists(self, proxyAddress):
        """reads proxy contract with address 'proxyAddress's storage at specific slot as defined in EIP 1967
        to obtain the implementation contract address."""
        impl_contract = Web3.toHex(
            self.w3.eth.get_storage_at(
                proxyAddress,
                "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
            )
        )
        if int(impl_contract, 16) != 0:
            return impl_contract
        else:
            return proxyAddress

class V3Factory(Contract):
    def __init__(self, tokenA, tokenB):
        super().__init__()
        self.address = os.getenv('UNI_FACTORY_V3')
        self.bips = [100, 500, 3000, 10000]
        self.tokenA = tokenA
        self.tokenB = tokenB
        # poolsdict is a dict of pool contract addresses (keys) to pool fees (values)
        self.poolsDict = self.getV3PairAddresses()

    def getV3PairAddresses(self):
        poolAddressesToFees = {}
        self.Abi = self.getAbi(self.address)
        self.Instance = self.w3.eth.contract(address = self.address, abi = self.Abi)
        for fee in self.bips:
            poolAddress = self.Instance.functions.getPool(self.tokenA, self.tokenB, fee).call()
        # getPool returns null address if no pool found
            if int(poolAddress, 16) != 0:
                poolAddressesToFees[poolAddress] = fee
        return poolAddressesToFees

class V2Factory(Contract):
    def __init__(self, tokenA, tokenB):
        super().__init__()
        self.address = os.getenv('UNI_FACTORY_V2')
        self.Abi = self.getAbi(self.address)
        self.Instance = self.w3.eth.contract(address = self.address, abi = self.Abi)
        self.V2poolAddress = self.Instance.functions.getPair(tokenA, tokenB).call()

class V2Pool(Contract):
    def __init__(self, address):
        super().__init__()
        self.uniVersion = 2
        self.address = address
        self.abi = self.getAbi(self.address)
        self.instance = self.w3.eth.contract(address = address, abi = self.abi)
        self.x = Token(self.instance.functions.token0.__call__().call()) # token 0 from pool contract
        self.y = Token(self.instance.functions.token1.__call__().call()) # token 1 from pool contract
        RAW_RESERVES = self.instance.functions.getReserves.__call__().call()
        # TVL = (PRICE[0] * RESERVE[0], PRICE[1] * RESERVE[1])
        self.xQuantity = self.normaliseDecimals(self.x, int(RAW_RESERVES[0]))
        self.yQuantity = self.normaliseDecimals(self.y, int(RAW_RESERVES[1]))
        self.xPrice = self.x.getTokenPrice()
        self.yPrice = self.y.getTokenPrice()
        self.xValue = self.xQuantity * self.xPrice
        self.yValue = self.yQuantity * self.yPrice
        try:
            self.priceRatio = self.xQuantity/self.yQuantity
        except ZeroDivisionError:
            self.priceRatio = 'NA'
    
    def normaliseDecimals(self, tokenObject, value):
        decimals = tokenObject.decimals
        value = value * pow(10,-decimals)
        value = float("{:.3f}".format(value))
        return value 

class V3Pool(Contract):
    """class for uni v3 liquidity pools. All/ most have same ABI which is hardcoded.
    V3 pools also have different functions to V2 pools, hence the separation
    inherits from contract class"""

    def __init__(self, address):
        super().__init__() # get attributes and methods from infra class
        self.uniVersion = 3
        self.address = address
        self.abi = abi = json.load(open("./v3PoolABI.json"))
        self.instance = self.w3.eth.contract(address = address, abi = abi)
        self.x = Token(self.instance.functions.token0.__call__().call()) # token 0 from pool contract
        self.y = Token(self.instance.functions.token1.__call__().call()) # token 1 from pool contract
        self.L = self.instance.functions.liquidity.__call__().call()
        self.tick = self.instance.functions.slot0().call()[1]
        self.tickSpacing = self.instance.functions.tickSpacing.__call__().call()
        self.sqrtP = self.tick_to_sqrtprice(self.tick)
        (self.lowerTick, self.upperTick) = self.findBoundaryTicks(self.tick,self.tickSpacing)
        (self.sqrtPb, self.sqrtPa) = self.sPriceFromTick((self.upperTick, self.lowerTick))
        self.yQuantity = (self.y_in_range(self.L, self.sqrtP, self.sqrtPa))/(10**self.y.decimals)
        self.xQuantity = (self.x_in_range(self.L, self.sqrtP, self.sqrtPb))/(10**self.x.decimals)
        self.xPrice = self.x.getTokenPrice()
        self.yPrice = self.y.getTokenPrice()
        self.xValue = self.xQuantity * self.xPrice
        self.yValue = self.yQuantity * self.yPrice
        try:
            self.priceRatio = self.xQuantity/self.yQuantity
        except ZeroDivisionError:
            self.priceRatio = 'NA'

    
    def findBoundaryTicks(self, currentTick, tickSpacing):
        # find lower boundary
        currentTickInt = math.floor(currentTick)   
        if currentTickInt % tickSpacing == 0:
            return (currentTickInt, (currentTickInt + tickSpacing))
        else:
            return self.findBoundaryTicks(currentTickInt-1, tickSpacing)
    
    def sPriceFromTick(self, ticks):
        sqrtPrices = []
        for tick in ticks:
            price = pow(1.0001, tick)
            sqrtPrices.append(math.sqrt(price))
        return sqrtPrices

            # amount of x in range; sp - sqrt of current price, sb - sqrt of max price
    def x_in_range(self, L, sp, sb):
        return L * (sb - sp) / (sp * sb)

    # amount of y in range; sp - sqrt of current price, sa - sqrt of min price
    def y_in_range(self, L, sp, sa):
        return L * (sp - sa)

    def tick_to_sqrtprice(self, tick):
        return math.sqrt(1.0001 ** tick)


class Token(Contract):
    """class for underlying pool tokens Inherits from 'contract' class"""

    def __init__(self, address):
        super().__init__()
        self.cg = CoinGeckoAPI()
        self.address = address
        self.abi = self.getAbi(address)
        self.instance = self.w3.eth.contract(address = address, abi = self.abi)
        self.decimals = self.instance.functions.decimals.__call__().call()

    def getTokenPrice(self):
        coinsList = self.cg.get_token_price(id = 'ethereum',contract_addresses = self.address, vs_currencies = 'usd')
        for key in coinsList.keys():
            return coinsList[key]["usd"]

def main():
    tokenA = '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48'
    tokenB = '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2'
    C = Contract()
    V3F = V3Factory(tokenA, tokenB)
    V2F = V2Factory(tokenA, tokenB)
    v2Pool = V2Pool(V2F.V2poolAddress)
    v3PoolsDict = {pool: V3Pool(pool) for pool in V3F.poolsDict.keys()}
    df = writeToDataFrame(v2Pool, V2F)
    for poolKey in v3PoolsDict.keys():
        df = pd.concat([df, writeToDataFrame(v3PoolsDict[poolKey], V3F)])
    f = open('./output.csv', 'w')
    df.to_csv(f, index=False)
    

def writeToDataFrame(poolInstance, Factory):
    if poolInstance.uniVersion == 3:
        fee = Factory.poolsDict[poolInstance.address]
    else:
        fee = 3000
    data = {
    "version number": [poolInstance.uniVersion], 
    "pool address": [poolInstance.address],
    "token0 contract address": [poolInstance.x.address],
    "token1 contract address": [poolInstance.y.address],
    "fee tier": [fee],
    "amount of token0 in pool": [poolInstance.xQuantity],
    "amount of token1 in pool": [poolInstance.yQuantity],
    "token0 TVL": [poolInstance.xValue],
    "token1 TVL": [poolInstance.yValue],
    "token0/token1": [poolInstance.priceRatio],
    "token0 price": [poolInstance.xPrice],
    "token1 price": [poolInstance.yPrice]
    }
    df = pd.DataFrame(data)
    print(df)
    return df



if __name__ == '__main__':
    main()