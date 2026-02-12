import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
    AlertTriangle,
    Check,
    CheckCircle2,
    ChevronsUpDown,
    Filter,
    Play,
    RefreshCw,
    Settings2,
    Square,
    TrendingUp,
    X,
} from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { optionChainApi } from '@/api/option-chain'
import { oiProfileApi } from '@/api/oi-profile'
import { useMarketData } from '@/hooks/useMarketData'
import { apiClient } from '@/api/client'
import type { OptionChainResponse } from '@/types/option-chain'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Switch } from '@/components/ui/switch'
import {
    Command,
    CommandEmpty,
    CommandGroup,
    CommandInput,
    CommandItem,
    CommandList,
} from '@/components/ui/command'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
    Popover,
    PopoverContent,
    PopoverTrigger,
} from '@/components/ui/popover'
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from '@/components/ui/select'
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { showToast } from '@/utils/toast'
import { cn } from '@/lib/utils'

// Types
interface MonitorConfig {
    exchange: string
    underlying: string
    expiry: string
    strikeCount: number
    distanceThreshold: number
    premiumThreshold: number
    ivThreshold: number
    ivRefreshSeconds: number
    spikeReference: 'OPEN' | 'PREV_CLOSE' | 'LAST_X_MIN'
    lastXMinutes: number
    spikeThresholdPercent: number
    skipIvWhenDistanceFail: boolean
}

interface MonitoredStrike {
    symbol: string
    type: 'CE' | 'PE'
    strike: number
    baseSymbol: string
}

interface StrikeStatus {
    distance: number
    currentPremium: number
    currentIv: number
    spikePercent: number
    lastTickTime: number
    isDistancePass: boolean
    isPremiumPass: boolean
    isIvPass: boolean
    isSpikePass: boolean
    isHistoryPass: boolean
    isAllPass: boolean
}

interface ReferenceSnapshot {
    price: number
    timestamp: number
}

interface IvSummary {
    status: 'success' | 'partial' | 'error'
    total: number
    success: number
    failed: number
}

const DEFAULT_CONFIG: MonitorConfig = {
    exchange: 'NFO',
    underlying: 'NIFTY',
    expiry: '',
    strikeCount: 10,
    distanceThreshold: 500,
    premiumThreshold: 5,
    ivThreshold: 30,
    ivRefreshSeconds: 30,
    spikeReference: 'OPEN',
    lastXMinutes: 5,
    spikeThresholdPercent: 10,
    skipIvWhenDistanceFail: false,
}

const FNO_EXCHANGES = [
    { value: 'NFO', label: 'NFO' },
    { value: 'BFO', label: 'BFO' },
]

export default function OptionSpikeMonitor() {
    const { apiKey } = useAuthStore()

    // Configuration State
    const [config, setConfig] = useState<MonitorConfig>(DEFAULT_CONFIG)
    const [isMonitoring, setIsMonitoring] = useState(false)
    const [isConfigOpen, setIsConfigOpen] = useState(true)

    // Data State
    const [underlyings, setUnderlyings] = useState<string[]>([])
    const [expiries, setExpiries] = useState<string[]>([])
    const [optionChain, setOptionChain] = useState<OptionChainResponse | null>(null)
    const [ivData, setIvData] = useState<Record<string, number>>({})
    const [ivSummary, setIvSummary] = useState<IvSummary | null>(null)
    const [tickTimes, setTickTimes] = useState<Record<string, number>>({})
    const [referenceSnapshots, setReferenceSnapshots] = useState<Record<string, ReferenceSnapshot>>({})
    const ivRetryTimeoutRef = useRef<number | null>(null)
    const ivRetrySymbolsRef = useRef<Record<string, { symbol: string; exchange: string }>>({})

    // Helper state
    const [underlyingOpen, setUnderlyingOpen] = useState(false)
    const [isLoadingChain, setIsLoadingChain] = useState(false)

    // Derived list of symbols to monitor (for WS subscription)
    const [monitoredStrikes, setMonitoredStrikes] = useState<MonitoredStrike[]>([])
    const wsSymbols = useMemo(() => {
        const symbols = monitoredStrikes.map(s => ({
            symbol: s.symbol,
            exchange: config.exchange
        }))
        // Add underlying for spot price
        symbols.push({
            symbol: config.underlying,
            exchange: config.exchange === 'NFO' ? 'NSE_INDEX' : 'BSE_INDEX' // Simple logic, might need refinement
        })
        return symbols
    }, [monitoredStrikes, config.exchange, config.underlying])

    // WebSocket Hook
    const { data: wsData } = useMarketData({
        symbols: wsSymbols,
        mode: 'LTP',
        enabled: isMonitoring && wsSymbols.length > 0
    })

    // Format helpers
    const formatPrice = (num: number | undefined) => num?.toFixed(2) ?? '0.00'

    const resolveReferenceLabel = () => {
        if (config.spikeReference === 'OPEN') return "Today's Open"
        if (config.spikeReference === 'PREV_CLOSE') return "Yesterday's Close"
        return `Last ${config.lastXMinutes} Minutes`
    }

    // Load Underlyings
    useEffect(() => {
        const fetchUnderlyings = async () => {
            try {
                const response = await oiProfileApi.getUnderlyings(config.exchange)
                if (response.status === 'success') {
                    setUnderlyings(response.underlyings)
                    // Set default if current not in list
                    if (!response.underlyings.includes(config.underlying)) {
                        const defaultUnderlying = config.exchange === 'BFO'
                            ? (response.underlyings.includes('SENSEX') ? 'SENSEX' : response.underlyings[0] || '')
                            : (response.underlyings[0] || '')
                        setConfig(prev => ({ ...prev, underlying: defaultUnderlying }))
                    }
                }
            } catch (err) {
                console.error('Failed to fetch underlyings', err)
            }
        }
        fetchUnderlyings()
    }, [config.exchange])

    // Load Expiries
    useEffect(() => {
        if (!config.underlying) return
        const fetchExpiries = async () => {
            try {
                const response = await oiProfileApi.getExpiries(config.exchange, config.underlying)
                if (response.status === 'success') {
                    setExpiries(response.expiries)
                    setConfig(prev => ({ ...prev, expiry: response.expiries[0] || '' }))
                }
            } catch (err) {
                console.error('Failed to fetch expiries', err)
            }
        }
        fetchExpiries()
    }, [config.exchange, config.underlying])

    const fetchReferenceSnapshot = useCallback(async (symbols: { symbol: string; exchange: string }[]) => {
        if (!apiKey) {
            return
        }

        const referenceTime = new Date(Date.now() - config.lastXMinutes * 60 * 1000)
        const startDate = referenceTime.toISOString().slice(0, 10)
        const endDate = new Date().toISOString().slice(0, 10)

        const snapshotUpdates: Record<string, ReferenceSnapshot> = {}
        for (const item of symbols) {
            try {
                const response = await apiClient.post('/history', {
                    apikey: apiKey,
                    symbol: item.symbol,
                    exchange: item.exchange,
                    interval: '1m',
                    start_date: startDate,
                    end_date: endDate,
                    source: 'api'
                })

                if (response.data.status === 'success' && response.data.data?.length) {
                    const candles = response.data.data
                    const cutoff = referenceTime.getTime()
                    let closest = candles[0]
                    for (const candle of candles) {
                        const candleTime = new Date(candle.timestamp ?? candle.date ?? candle.time).getTime()
                        if (candleTime <= cutoff) {
                            closest = candle
                        }
                    }
                    snapshotUpdates[item.symbol] = {
                        price: Number(closest.close ?? closest.c ?? closest.last_price ?? 0),
                        timestamp: cutoff
                    }
                }
            } catch (error) {
                console.error('[OptionSpikeMonitor] Failed to fetch reference snapshot', item.symbol, error)
            }
        }

        if (Object.keys(snapshotUpdates).length > 0) {
            setReferenceSnapshots(prev => ({ ...prev, ...snapshotUpdates }))
        }
    }, [apiKey, config.lastXMinutes])

    // Fetch IV Data (Multi-Option Greeks)
    const fetchIvData = useCallback(async (overrideSymbols?: { symbol: string; exchange: string }[]) => {
        if (!apiKey) {
            console.log('[OptionSpikeMonitor] fetchIvData SKIPPED - no API key')
            return
        }
        if (!overrideSymbols && monitoredStrikes.length === 0) {
            console.log('[OptionSpikeMonitor] fetchIvData SKIPPED - no monitored strikes')
            return
        }

        try {
            const spotPrice = wsData.get(
                `${config.exchange === 'NFO' ? 'NSE_INDEX' : 'BSE_INDEX'}:${config.underlying}`
            )?.data?.ltp ?? optionChain?.underlying_ltp

            const symbols = overrideSymbols && overrideSymbols.length > 0
                ? overrideSymbols
                : monitoredStrikes
                    .filter(s => {
                        if (!config.skipIvWhenDistanceFail || spotPrice === undefined) {
                            return true
                        }
                        const distance = Math.abs(spotPrice - s.strike)
                        return distance > config.distanceThreshold
                    })
                    .map(s => ({
                        symbol: s.symbol,
                        exchange: config.exchange
                    }))

            if (symbols.length === 0) {
                return
            }

            // Batch in chunks of 20 to avoid payload limits if any
            // But typically 10-20 strikes is fine in one go
            const response = await apiClient.post('/multioptiongreeks', {
                apikey: apiKey,
                symbols: symbols
            })

            console.log('[OptionSpikeMonitor] fetchIvData request:', symbols)
            console.log('[OptionSpikeMonitor] fetchIvData response:', response.data)

            if ((response.data.status === 'success' || response.data.status === 'partial') && response.data.data) {
                const newIvData: Record<string, number> = {}
                const successSymbols = new Set<string>()
                response.data.data.forEach((item: any) => {
                    if (item.status === 'success' && item.implied_volatility !== undefined && item.symbol) {
                        newIvData[item.symbol] = item.implied_volatility
                        successSymbols.add(item.symbol)
                    }
                })
                if (Object.keys(newIvData).length > 0) {
                    setIvData(prev => ({ ...prev, ...newIvData }))
                }
                setIvSummary({
                    status: response.data.status,
                    total: response.data.summary?.total ?? response.data.data.length,
                    success: response.data.summary?.success ?? Object.keys(newIvData).length,
                    failed: response.data.summary?.failed ?? response.data.data.length - Object.keys(newIvData).length
                })

                if (response.data.status === 'partial') {
                    const failedSymbols = symbols.filter(sym => !successSymbols.has(sym.symbol))
                    if (failedSymbols.length > 0) {
                        const retryMap: Record<string, { symbol: string; exchange: string }> = {}
                        failedSymbols.forEach(sym => {
                            retryMap[sym.symbol] = sym
                        })
                        ivRetrySymbolsRef.current = retryMap
                        if (ivRetryTimeoutRef.current) {
                            window.clearTimeout(ivRetryTimeoutRef.current)
                        }
                        ivRetryTimeoutRef.current = window.setTimeout(() => {
                            const retrySymbols = Object.values(ivRetrySymbolsRef.current)
                            if (retrySymbols.length > 0) {
                                fetchIvData(retrySymbols)
                            }
                        }, 5000)
                    }
                }
            }
        } catch (err) {
            console.error('[OptionSpikeMonitor] Error fetching IV data', err)
        }
    }, [apiKey, monitoredStrikes, config.exchange])

    // Start Monitoring Logic
    const handleStart = async () => {
        if (!apiKey || !config.expiry) {
            showToast.error('Please configure expiry and ensure API key is set')
            return
        }

        setIsLoadingChain(true)
        setIsConfigOpen(false) // Auto collapse config

        try {
            // 1. Fetch Option Chain
            const expiryFormatted = config.expiry.split('-').length === 3
                ? `${config.expiry.split('-')[0]}${config.expiry.split('-')[1].toUpperCase()}${config.expiry.split('-')[2].slice(-2)}`
                : config.expiry

            const chainResponse = await optionChainApi.getOptionChain(
                apiKey,
                config.underlying,
                config.exchange,
                expiryFormatted,
                config.strikeCount
            )

            if (chainResponse && chainResponse.chain) {
                setOptionChain(chainResponse)

                // Filter strikes (OTM Only) based on ATM
                const atm = chainResponse.atm_strike
                const strikes: MonitoredStrike[] = []

                chainResponse.chain.forEach(s => {
                    // CE OTM: Strike > ATM
                    if (s.strike > atm && s.ce) {
                        strikes.push({
                            symbol: s.ce.symbol,
                            type: 'CE',
                            strike: s.strike,
                            baseSymbol: config.underlying
                        })
                    }
                    // PE OTM: Strike < ATM
                    if (s.strike < atm && s.pe) {
                        strikes.push({
                            symbol: s.pe.symbol,
                            type: 'PE',
                            strike: s.strike,
                            baseSymbol: config.underlying
                        })
                    }
                })

                setMonitoredStrikes(strikes)

                // Start Monitoring
                setIsMonitoring(true)

                // Reset tick times
                setTickTimes({})
                setReferenceSnapshots({})

                if (config.spikeReference === 'LAST_X_MIN') {
                    const historySymbols = strikes.map(strike => ({
                        symbol: strike.symbol,
                        exchange: config.exchange
                    }))
                    fetchReferenceSnapshot(historySymbols)
                }

                // Initial IV Fetch
                console.log('[OptionSpikeMonitor] Scheduling initial IV fetch in 1s, apiKey available:', !!apiKey)
                setTimeout(fetchIvData, 1000)
            } else {
                showToast.error('No option chain data found')
            }

        } catch (err) {
            console.error('Error starting monitor', err)
            showToast.error('Failed to start monitoring')
        } finally {
            setIsLoadingChain(false)
        }
    }

    const handleStop = () => {
        setIsMonitoring(false)
        setIsConfigOpen(true)
        setReferenceSnapshots({})
    }

    // Periodic Tasks (IV Fetch & Tick Clean up)
    useEffect(() => {
        if (!isMonitoring) return

        const refreshMs = Math.max(config.ivRefreshSeconds, 5) * 1000
        const intervalId = setInterval(() => {
            fetchIvData()
        }, refreshMs) // Fetch IV based on user config

        return () => {
            clearInterval(intervalId)
            if (ivRetryTimeoutRef.current) {
                window.clearTimeout(ivRetryTimeoutRef.current)
            }
        }
    }, [isMonitoring, monitoredStrikes, config.ivRefreshSeconds, fetchIvData])

    useEffect(() => {
        if (!isMonitoring || config.spikeReference !== 'LAST_X_MIN') {
            return
        }

        const historySymbols = monitoredStrikes.map(strike => ({
            symbol: strike.symbol,
            exchange: config.exchange
        }))

        if (historySymbols.length === 0) {
            return
        }

        fetchReferenceSnapshot(historySymbols)
    }, [config.spikeReference, config.lastXMinutes, isMonitoring, monitoredStrikes, config.exchange, fetchReferenceSnapshot])

    // Track WS Ticks
    useEffect(() => {
        if (!isMonitoring) return

        // Update tick times when WS data updates
        wsData.forEach((data, key) => {
            const symbol = key.split(':')[1]
            if (data.lastUpdate) {
                setTickTimes(prev => ({
                    ...prev,
                    [symbol]: data.lastUpdate ?? Date.now()
                }))
            }
        })
    }, [wsData, isMonitoring])


    // Calculate Table Rows
    const tableRows = useMemo(() => {
        if (!optionChain || !monitoredStrikes.length) return []

        const spotPrice = wsData.get(
            `${config.exchange === 'NFO' ? 'NSE_INDEX' : 'BSE_INDEX'}:${config.underlying}`
        )?.data?.ltp ?? optionChain.underlying_ltp

        const rows: (MonitoredStrike & StrikeStatus)[] = monitoredStrikes.map(s => {
            const wsKey = `${config.exchange}:${s.symbol}`
            const ltp = wsData.get(wsKey)?.data?.ltp ?? 0

            const distance = Math.abs(spotPrice - s.strike)
            const currentIv = ivData[s.symbol]
            // Spike Calc: ((Current - Ref) / Ref) * 100
            // NOTE: Reference price logic is currently based on Underlying. 
            // Requirement says "Spike % Threshold: Minimum percentage spike from the reference price."
            // Typically spike monitor checks OPTION PREMIUM spike.
            // So Reference Price should be the Reference Price of the OPTION, not Underlying?
            // Re-reading requirements: "Spike % Threshold: ... Calculated using historical data API for the reference price."
            // Reference options: Today's Open, Yesterday's Close.
            // This usually implies fetching OHLC for the *Option Symbol*.
            // My fetchReferencePrice above fetched Underlying. I should fetch for *each option*?
            // Fetching history for 20+ options might be heavy. But necessary for correct Spike %.
            // Let's assume for V1 we used 0 as placeholder or implement per-symbol fetch if feasible.

            // Correction: To be accurate, we need the reference price FOR THE OPTION.
            // We can't fetch history for 20 symbols efficiently in one go unless we loop.
            // OR allow "Reference" to be "Underlying Spike" -> but likely user wants Option Spike.
            // Ideally we fetch Prev Close from Option Chain itself (it has 'prev_close' and 'open').
            // Let's use OptionChain data for Open/PrevClose if available to avoid API spam.

            // Finding initial static data from chain
            const chainItem = optionChain.chain.find(i => i.strike === s.strike)
            const optionData = s.type === 'CE' ? chainItem?.ce : chainItem?.pe

            let optionRefPrice = 0
            if (config.spikeReference === 'OPEN') optionRefPrice = optionData?.open ?? 0
            else if (config.spikeReference === 'PREV_CLOSE') optionRefPrice = optionData?.prev_close ?? 0
            else {
                optionRefPrice = referenceSnapshots[s.symbol]?.price ?? 0
            }

            // If Ref Price is 0 (e.g. no trade today), use Prev Close
            if (optionRefPrice === 0) optionRefPrice = optionData?.prev_close ?? 0

            const spikePercent = optionRefPrice > 0 ? ((ltp - optionRefPrice) / optionRefPrice) * 100 : 0

            const lastTick = tickTimes[s.symbol] ?? 0
            const isLive = Date.now() - lastTick < 30000 // 30s heartbeat

            const isDistancePass = distance > config.distanceThreshold
            const isPremiumPass = ltp > config.premiumThreshold
            const isIvPass = currentIv !== undefined && currentIv > config.ivThreshold
            const isSpikePass = spikePercent > config.spikeThresholdPercent
            const isHistoryPass = isLive

            const isAllPass = isDistancePass && isPremiumPass && isIvPass && isSpikePass && isHistoryPass

            return {
                ...s,
                distance,
                currentPremium: ltp,
                currentIv: currentIv ?? 0,
                spikePercent,
                lastTickTime: lastTick,
                isDistancePass,
                isPremiumPass,
                isIvPass,
                isSpikePass,
                isHistoryPass,
                isAllPass
            }
        })

        return rows
            .filter(row => row.isDistancePass)
            .sort((a, b) => {
                if (a.isAllPass !== b.isAllPass) {
                    return a.isAllPass ? -1 : 1
                }
                return a.strike - b.strike
            })
    }, [monitoredStrikes, wsData, optionChain, tickTimes, ivData, config])

    const hiddenDistanceCount = useMemo(() => {
        if (!optionChain || !monitoredStrikes.length) return 0

        const spotPrice = wsData.get(
            `${config.exchange === 'NFO' ? 'NSE_INDEX' : 'BSE_INDEX'}:${config.underlying}`
        )?.data?.ltp ?? optionChain.underlying_ltp

        return monitoredStrikes.filter(s => Math.abs(spotPrice - s.strike) <= config.distanceThreshold).length
    }, [monitoredStrikes, wsData, optionChain, config])

    return (
        <div className="py-6 space-y-6">
            {/* Configuration Panel */}
            <Card className="w-full">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-4">
                    <CardTitle className="flex items-center gap-2">
                        <Settings2 className="h-5 w-5" />
                        Configuration
                    </CardTitle>
                    <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setIsConfigOpen(!isConfigOpen)}
                    >
                        {isConfigOpen ? <ChevronsUpDown className="h-4 w-4 rotate-180" /> : <ChevronsUpDown className="h-4 w-4" />}
                    </Button>
                </CardHeader>
                {isConfigOpen && (
                    <CardContent>
                        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
                            <div className="space-y-2">
                                <Label>Exchange & Underlying</Label>
                                <div className="flex gap-2">
                                    <Select value={config.exchange} onValueChange={(v) => setConfig(p => ({ ...p, exchange: v }))}>
                                        <SelectTrigger className="w-24">
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            {FNO_EXCHANGES.map(e => <SelectItem key={e.value} value={e.value}>{e.label}</SelectItem>)}
                                        </SelectContent>
                                    </Select>
                                    <Popover open={underlyingOpen} onOpenChange={setUnderlyingOpen}>
                                        <PopoverTrigger asChild>
                                            <Button variant="outline" role="combobox" aria-expanded={underlyingOpen} className="flex-1 justify-between">
                                                {config.underlying || "Select"}
                                                <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                                            </Button>
                                        </PopoverTrigger>
                                        <PopoverContent className="w-[200px] p-0">
                                            <Command>
                                                <CommandInput placeholder="Search..." />
                                                <CommandList>
                                                    <CommandEmpty>No underlying found.</CommandEmpty>
                                                    <CommandGroup>
                                                        {underlyings.map(u => (
                                                            <CommandItem key={u} value={u} onSelect={() => {
                                                                setConfig(p => ({ ...p, underlying: u }))
                                                                setUnderlyingOpen(false)
                                                            }}>
                                                                <Check className={cn("mr-2 h-4 w-4", config.underlying === u ? "opacity-100" : "opacity-0")} />
                                                                {u}
                                                            </CommandItem>
                                                        ))}
                                                    </CommandGroup>
                                                </CommandList>
                                            </Command>
                                        </PopoverContent>
                                    </Popover>
                                </div>
                            </div>

                            <div className="space-y-2">
                                <Label>Expiry & Strikes</Label>
                                <div className="flex gap-2">
                                    <Select value={config.expiry} onValueChange={(v) => setConfig(p => ({ ...p, expiry: v }))}>
                                        <SelectTrigger className="flex-1">
                                            <SelectValue placeholder="Expiry" />
                                        </SelectTrigger>
                                        <SelectContent>
                                            {expiries.map(e => <SelectItem key={e} value={e}>{e}</SelectItem>)}
                                        </SelectContent>
                                    </Select>
                                    <Select value={String(config.strikeCount)} onValueChange={(v) => setConfig(p => ({ ...p, strikeCount: Number(v) }))}>
                                        <SelectTrigger className="w-24">
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            {[5, 10, 15, 20, 25].map(n => <SelectItem key={n} value={String(n)}>{n} str</SelectItem>)}
                                        </SelectContent>
                                    </Select>
                                </div>
                            </div>

                            <div className="space-y-2">
                                <Label>Filters ( &gt; )</Label>
                                <div className="grid grid-cols-2 gap-2">
                                    <div className="space-y-1">
                                        <span className="text-xs text-muted-foreground">Dist (Pts)</span>
                                        <Input
                                            type="number"
                                            value={config.distanceThreshold}
                                            onChange={e => setConfig(p => ({ ...p, distanceThreshold: Number(e.target.value) }))}
                                        />
                                    </div>
                                    <div className="space-y-1">
                                        <span className="text-xs text-muted-foreground">Prem (Min)</span>
                                        <Input
                                            type="number"
                                            value={config.premiumThreshold}
                                            onChange={e => setConfig(p => ({ ...p, premiumThreshold: Number(e.target.value) }))}
                                        />
                                    </div>
                                    <div className="col-span-2 flex items-center justify-between rounded-md border px-3 py-2">
                                        <div>
                                            <p className="text-xs font-medium">Skip IV if distance fails</p>
                                            <p className="text-[11px] text-muted-foreground">Reduce IV calls for near strikes</p>
                                        </div>
                                        <Switch
                                            checked={config.skipIvWhenDistanceFail}
                                            onCheckedChange={(checked) => setConfig(p => ({ ...p, skipIvWhenDistanceFail: checked }))}
                                        />
                                    </div>
                                </div>
                            </div>

                            <div className="space-y-2">
                                <Label>Thresh ( &gt; ) & Spike</Label>
                                <div className="grid grid-cols-2 gap-2">
                                    <div className="space-y-1">
                                        <span className="text-xs text-muted-foreground">IV (Max)</span>
                                        <Input
                                            type="number"
                                            value={config.ivThreshold}
                                            onChange={e => setConfig(p => ({ ...p, ivThreshold: Number(e.target.value) }))}
                                        />
                                    </div>
                                    <div className="space-y-1">
                                        <span className="text-xs text-muted-foreground">IV Refresh (sec)</span>
                                        <Input
                                            type="number"
                                            min={5}
                                            value={config.ivRefreshSeconds}
                                            onChange={e => setConfig(p => ({ ...p, ivRefreshSeconds: Number(e.target.value) }))}
                                        />
                                    </div>
                                    <div className="space-y-1 col-span-2">
                                        <span className="text-xs text-muted-foreground">Spike %</span>
                                        <Input
                                            type="number"
                                            value={config.spikeThresholdPercent}
                                            onChange={e => setConfig(p => ({ ...p, spikeThresholdPercent: Number(e.target.value) }))}
                                        />
                                    </div>
                                </div>
                            </div>

                            <div className="space-y-2 lg:col-span-4">
                                <Label>Spike Reference</Label>
                                <div className="flex gap-4 items-center">
                                    <Select value={config.spikeReference} onValueChange={(v: any) => setConfig(p => ({ ...p, spikeReference: v }))}>
                                        <SelectTrigger className="w-48">
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="OPEN">Today's Open</SelectItem>
                                            <SelectItem value="PREV_CLOSE">Yesterday's Close</SelectItem>
                                            <SelectItem value="LAST_X_MIN">Last X Minutes</SelectItem>
                                        </SelectContent>
                                    </Select>

                                    {config.spikeReference === 'LAST_X_MIN' && (
                                        <div className="flex items-center gap-2">
                                            <Input
                                                type="number"
                                                className="w-20"
                                                value={config.lastXMinutes}
                                                onChange={e => setConfig(p => ({ ...p, lastXMinutes: Number(e.target.value) }))}
                                            />
                                            <span className="text-sm">mins ago</span>
                                        </div>
                                    )}
                                </div>
                            </div>

                        </div>

                        <div className="flex justify-end gap-2 mt-6">
                            <Button
                                variant={isMonitoring ? "destructive" : "default"}
                                onClick={isMonitoring ? handleStop : handleStart}
                                disabled={isLoadingChain}
                            >
                                {isLoadingChain ? <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> :
                                    isMonitoring ? <Square className="mr-2 h-4 w-4 fill-current" /> :
                                        <Play className="mr-2 h-4 w-4 fill-current" />}
                                {isMonitoring ? "Stop Monitor" : "Start Monitor"}
                            </Button>
                        </div>
                    </CardContent>
                )}
            </Card>

            {/* Main Content */}
            {isMonitoring && optionChain && (
                <div className="space-y-4">

                    {/* Status Cards */}
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                        <Card>
                            <CardContent className="p-4 flex items-center justify-between">
                                <div>
                                    <p className="text-sm font-medium text-muted-foreground">Spot Price</p>
                                    <h2 className="text-2xl font-bold">
                                        {formatPrice(
                                            wsData.get(`${config.exchange === 'NFO' ? 'NSE_INDEX' : 'BSE_INDEX'}:${config.underlying}`)?.data?.ltp
                                            ?? optionChain.underlying_ltp
                                        )}
                                    </h2>
                                </div>
                                <TrendingUp className="h-8 w-8 text-primary opacity-50" />
                            </CardContent>
                        </Card>
                        <Card>
                            <CardContent className="p-4 flex items-center justify-between">
                                <div>
                                    <p className="text-sm font-medium text-muted-foreground">Reference Basis</p>
                                    <h2 className="text-xl font-bold">
                                        {resolveReferenceLabel()}
                                    </h2>
                                    <p className="text-xs text-muted-foreground">
                                        {config.spikeReference === 'LAST_X_MIN'
                                            ? 'Using option history'
                                            : "Using option's static data"}
                                    </p>
                                </div>
                                <Filter className="h-8 w-8 text-blue-500 opacity-50" />
                            </CardContent>
                        </Card>
                        <Card>
                            <CardContent className="p-4 flex items-center justify-between">
                                <div>
                                    <p className="text-sm font-medium text-muted-foreground">Monitored Strikes</p>
                                    <h2 className="text-2xl font-bold">{monitoredStrikes.length}</h2>
                                    <p className="text-xs text-green-500">
                                        {tableRows.filter(r => r.isAllPass).length} Passing
                                    </p>
                                    {hiddenDistanceCount > 0 && (
                                        <p className="text-xs text-amber-600">
                                            {hiddenDistanceCount} Hidden (distance)
                                        </p>
                                    )}
                                </div>
                                <CheckCircle2 className="h-8 w-8 text-green-500 opacity-50" />
                            </CardContent>
                        </Card>
                    </div>

                    {/* Monitor Table */}
                    <Card>
                        <Table>
                            <TableHeader>
                                <TableRow>
                                    <TableHead>SYMBOL</TableHead>
                                    <TableHead className="text-center">TYPE</TableHead>
                                    <TableHead className="text-center">STRIKE</TableHead>
                                    <TableHead className="text-right">PREMIUM</TableHead>
                                    <TableHead className="text-center">DISTANCE</TableHead>
                                    <TableHead className="text-center">PREM &gt; {config.premiumThreshold}</TableHead>
                                    <TableHead className="text-center">
                                        <div className="flex items-center justify-center gap-2">
                                            <span>IV &gt; {config.ivThreshold}</span>
                                            {ivSummary && ivSummary.status === 'partial' && (
                                                <Badge
                                                    variant="outline"
                                                    className="text-[10px] uppercase text-amber-600 border-amber-500/40 bg-amber-500/10"
                                                >
                                                    partial {ivSummary.success}/{ivSummary.total}
                                                </Badge>
                                            )}
                                        </div>
                                    </TableHead>
                                    <TableHead className="text-center">HISTORY</TableHead>
                                    <TableHead className="text-center">SPIKE &gt; {config.spikeThresholdPercent}%</TableHead>
                                    <TableHead className="text-center">ALL PASS</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {tableRows.map((row) => (
                                    <TableRow key={row.symbol} className={row.isAllPass ? 'bg-green-500/10' : ''}>
                                        <TableCell className="font-medium text-xs">{row.symbol}</TableCell>
                                        <TableCell className="text-center">
                                            <Badge variant="outline" className={row.type === 'CE' ? 'bg-green-500/10 text-green-500 border-green-500/20' : 'bg-red-500/10 text-red-500 border-red-500/20'}>
                                                {row.type}
                                            </Badge>
                                        </TableCell>
                                        <TableCell className="text-center font-bold">{row.strike}</TableCell>
                                        <TableCell className="text-right font-mono text-base font-semibold">
                                            {formatPrice(row.currentPremium)}
                                        </TableCell>
                                        <TableCell className="text-center">
                                            <div className={cn("text-xs font-medium", row.isDistancePass ? "text-green-500" : "text-red-500")}>
                                                {formatPrice(row.distance)}
                                            </div>
                                        </TableCell>
                                        <TableCell className="text-center">
                                            {row.isPremiumPass ? <Check className="h-4 w-4 mx-auto text-green-500" /> : <X className="h-4 w-4 mx-auto text-red-500" />}
                                        </TableCell>
                                        <TableCell className="text-center">
                                            <div className={cn("text-xs font-medium", row.isIvPass ? "text-green-500" : "text-red-500")}>
                                                {row.currentIv.toFixed(2)}%
                                            </div>
                                        </TableCell>
                                        <TableCell className="text-center">
                                            {row.isHistoryPass ?
                                                <CheckCircle2 className="h-4 w-4 mx-auto text-green-500 animate-pulse" /> :
                                                <AlertTriangle className="h-4 w-4 mx-auto text-red-500" />
                                            }
                                        </TableCell>
                                        <TableCell className="text-center">
                                            <div className={cn("font-bold text-xs", row.isSpikePass ? "text-green-500" : "text-red-500")}>
                                                {row.spikePercent.toFixed(1)}%
                                            </div>
                                        </TableCell>
                                        <TableCell className="text-center">
                                            {row.isAllPass ?
                                                <div className="flex justify-center"><div className="h-6 w-6 rounded-full bg-green-500 flex items-center justify-center text-white"><Check className="h-4 w-4" /></div></div> :
                                                <div className="flex justify-center"><div className="h-6 w-6 rounded-full bg-red-500/20 flex items-center justify-center text-red-500"><X className="h-4 w-4" /></div></div>
                                            }
                                        </TableCell>
                                    </TableRow>
                                ))}
                                {tableRows.length === 0 && (
                                    <TableRow>
                                        <TableCell colSpan={10} className="h-24 text-center">
                                            No strikes match the criteria or market is closed.
                                        </TableCell>
                                    </TableRow>
                                )}
                            </TableBody>
                        </Table>
                    </Card>
                </div>
            )}

            {!isMonitoring && !optionChain && (
                <div className="flex flex-col items-center justify-center py-12 text-muted-foreground space-y-4">
                    <div className="h-16 w-16 bg-muted rounded-full flex items-center justify-center">
                        <Settings2 className="h-8 w-8 opacity-50" />
                    </div>
                    <h3 className="text-lg font-medium">Ready to Monitor</h3>
                    <p className="max-w-sm text-center">Configure the parameters above and click 'Start Monitor' to begin tracking option spikes.</p>
                </div>
            )}
        </div>
    )
}
