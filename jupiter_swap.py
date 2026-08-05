# 1- get_quote(USDC->TOKEN, amount)
# 2- build_swap_transaction(quote,pubkey)
# 3- sign_and_send(raw_tx, keypair)
# 4- confirm_transaction(signature)


# If a sell fail due to low liquidity, we escalate slippage from 200 to 500 to 1000 basis points before giving up
