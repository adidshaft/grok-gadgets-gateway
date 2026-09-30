from grok_gadgets_gateway.demo import acceptance


async def test_real_official_mcp_client_simulator_acceptance():
    await acceptance()


async def test_simulation_controls_absent_by_default():
    await acceptance(False)
