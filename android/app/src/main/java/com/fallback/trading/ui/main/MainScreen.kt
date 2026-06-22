package com.fallback.trading.ui.main

import android.Manifest
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.core.tween
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import com.fallback.trading.ui.theme.BrandTeal
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.ShowChart
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.outlined.AccountBalanceWallet
import androidx.compose.material.icons.outlined.AutoAwesome
import androidx.compose.material.icons.outlined.BarChart
import androidx.compose.material.icons.outlined.Groups
import androidx.compose.material.icons.outlined.History
import androidx.compose.material.icons.outlined.MoreVert
import androidx.compose.material.icons.outlined.NotificationsNone
import androidx.compose.material.icons.outlined.PersonAdd
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.fallback.trading.AppContainer
import com.fallback.trading.data.AdminUser
import com.fallback.trading.data.TradingScope
import com.fallback.trading.ui.admin.CreateUserSheet
import com.fallback.trading.ui.analytics.AnalyticsScreen
import com.fallback.trading.ui.history.HistoryScreen
import com.fallback.trading.ui.intelligence.IntelligenceScreen
import com.fallback.trading.ui.portfolio.PortfolioScreen
import com.fallback.trading.ui.positions.PositionsScreen
import com.fallback.trading.ui.trade.TradeSheet
import com.fallback.trading.ui.trade.TradeViewModel
import com.fallback.trading.ui.update.UpdateHost
import com.fallback.trading.ui.update.UpdateViewModel
import kotlinx.coroutines.launch

private enum class MainTab(val route: String, val label: String, val icon: ImageVector) {
    Portfolio("portfolio", "Portfolio", Icons.Outlined.AccountBalanceWallet),
    Positions("positions", "Positions", Icons.AutoMirrored.Outlined.ShowChart),
    History("history", "History", Icons.Outlined.History),
    Analytics("analytics", "Analysis", Icons.Outlined.BarChart),
    AI("intel", "AI", Icons.Outlined.AutoAwesome),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen(
    container: AppContainer,
    onSignedOut: () -> Unit,
) {
    val navController = rememberNavController()
    val scope = rememberCoroutineScope()
    var menuOpen by remember { mutableStateOf(false) }

    val tradeViewModel: TradeViewModel = viewModel(factory = TradeViewModel.factory(container))
    var sheetOpen by remember { mutableStateOf(false) }
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    var createUserSheetOpen by remember { mutableStateOf(false) }
    val createUserSheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)

    val updateViewModel: UpdateViewModel = viewModel(factory = UpdateViewModel.factory(container))
    LaunchedEffect(Unit) { updateViewModel.checkOnLaunch() }

    val backStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = backStackEntry?.destination?.route
    val currentTab = MainTab.entries.firstOrNull { it.route == currentRoute } ?: MainTab.Portfolio

    val isAdmin by container.repository.adminState.isAdmin.collectAsStateWithLifecycle()
    val adminUsers by container.repository.adminState.users.collectAsStateWithLifecycle()
    val tradingScope by container.repository.adminState.scope.collectAsStateWithLifecycle()

    val notifyOpened by container.settings.notifyTradeOpened.collectAsStateWithLifecycle(initialValue = true)
    val notifyClosed by container.settings.notifyTradeClosed.collectAsStateWithLifecycle(initialValue = true)

    // Request POST_NOTIFICATIONS permission on first MainScreen entry.
    val permLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    LaunchedEffect(Unit) { permLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) }

    val onSessionExpired: () -> Unit = {
        scope.launch {
            container.repository.logout()
            onSignedOut()
        }
    }

    fun openTrade(symbol: String? = null) {
        symbol?.let(tradeViewModel::setSymbol)
        sheetOpen = true
    }

    fun dismissSheet() {
        scope.launch { sheetState.hide() }.invokeOnCompletion { sheetOpen = false }
    }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                title = {
                    if (currentTab == MainTab.Portfolio) {
                        Text(
                            "SAIT",
                            fontWeight = FontWeight.ExtraBold,
                            color = BrandTeal,
                            style = MaterialTheme.typography.headlineMedium,
                        )
                    } else {
                        Text(currentTab.label, fontWeight = FontWeight.Bold)
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                ),
                actions = {
                    if (isAdmin) {
                        AdminScopeSelector(
                            scope = tradingScope,
                            users = adminUsers,
                            onSelect = { container.repository.adminState.setScope(it) },
                        )
                    }
                    IconButton(onClick = { menuOpen = true }) {
                        Icon(Icons.Outlined.MoreVert, contentDescription = "More")
                    }
                    DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                        if (isAdmin) {
                            DropdownMenuItem(
                                text = { Text("Create user") },
                                leadingIcon = { Icon(Icons.Outlined.PersonAdd, contentDescription = null) },
                                onClick = {
                                    menuOpen = false
                                    createUserSheetOpen = true
                                },
                            )
                            HorizontalDivider()
                        }
                        DropdownMenuItem(
                            text = { Text("Notify: trade opened") },
                            leadingIcon = { Icon(Icons.Outlined.NotificationsNone, contentDescription = null) },
                            trailingIcon = {
                                Switch(
                                    checked = notifyOpened,
                                    onCheckedChange = { scope.launch { container.settings.setNotifyTradeOpened(it) } },
                                )
                            },
                            onClick = { scope.launch { container.settings.setNotifyTradeOpened(!notifyOpened) } },
                        )
                        DropdownMenuItem(
                            text = { Text("Notify: trade closed") },
                            leadingIcon = { Icon(Icons.Outlined.NotificationsNone, contentDescription = null) },
                            trailingIcon = {
                                Switch(
                                    checked = notifyClosed,
                                    onCheckedChange = { scope.launch { container.settings.setNotifyTradeClosed(it) } },
                                )
                            },
                            onClick = { scope.launch { container.settings.setNotifyTradeClosed(!notifyClosed) } },
                        )
                        HorizontalDivider()
                        DropdownMenuItem(
                            text = { Text("Check for updates") },
                            onClick = {
                                menuOpen = false
                                updateViewModel.check(manual = true)
                            },
                        )
                        DropdownMenuItem(
                            text = { Text("Sign out") },
                            onClick = {
                                menuOpen = false
                                onSessionExpired()
                            },
                        )
                    }
                },
            )
        },
        bottomBar = {
            AppBottomBar(
                current = currentTab,
                onSelect = { tab ->
                    if (currentTab != tab) {
                        navController.navigate(tab.route) {
                            popUpTo(navController.graph.findStartDestination().id) { saveState = true }
                            launchSingleTop = true
                            restoreState = true
                        }
                    }
                },
            )
        },
    ) { innerPadding ->
        NavHost(
            navController = navController,
            startDestination = MainTab.Portfolio.route,
            modifier = Modifier.padding(innerPadding),
            enterTransition = { fadeIn(tween(220)) },
            exitTransition = { fadeOut(tween(180)) },
        ) {
            composable(MainTab.Portfolio.route) {
                PortfolioScreen(container, onTrade = { openTrade() }, onSessionExpired = onSessionExpired)
            }
            composable(MainTab.Positions.route) {
                PositionsScreen(container, onTrade = { openTrade(it) }, onSessionExpired = onSessionExpired)
            }
            composable(MainTab.History.route) {
                HistoryScreen(container, onSessionExpired = onSessionExpired)
            }
            composable(MainTab.Analytics.route) {
                AnalyticsScreen(container, onSessionExpired = onSessionExpired)
            }
            composable(MainTab.AI.route) {
                IntelligenceScreen(container, onSessionExpired = onSessionExpired)
            }
        }
    }

    if (sheetOpen) {
        ModalBottomSheet(
            onDismissRequest = { sheetOpen = false },
            sheetState = sheetState,
            containerColor = MaterialTheme.colorScheme.surface,
        ) {
            TradeSheet(
                viewModel = tradeViewModel,
                onSessionExpired = {
                    sheetOpen = false
                    onSessionExpired()
                },
                onClose = { dismissSheet() },
            )
        }
    }

    if (createUserSheetOpen) {
        ModalBottomSheet(
            onDismissRequest = { createUserSheetOpen = false },
            sheetState = createUserSheetState,
            containerColor = MaterialTheme.colorScheme.surface,
        ) {
            CreateUserSheet(
                repository = container.repository,
                onSessionExpired = {
                    createUserSheetOpen = false
                    onSessionExpired()
                },
            )
        }
    }

    UpdateHost(updateViewModel)
}

@Composable
private fun AppBottomBar(
    current: MainTab,
    onSelect: (MainTab) -> Unit,
) {
    Surface(
        color = MaterialTheme.colorScheme.surface,
        shadowElevation = 12.dp,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .navigationBarsPadding()
                .height(64.dp)
                .padding(horizontal = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            BarItem(MainTab.Portfolio, current, onSelect)
            BarItem(MainTab.Positions, current, onSelect)
            BarItem(MainTab.History, current, onSelect)
            BarItem(MainTab.Analytics, current, onSelect)
            BarItem(MainTab.AI, current, onSelect)
        }
    }
}

@Composable
private fun androidx.compose.foundation.layout.RowScope.BarItem(
    tab: MainTab,
    current: MainTab,
    onSelect: (MainTab) -> Unit,
) {
    val selected = tab == current
    val tint = if (selected) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant
    Column(
        modifier = Modifier
            .weight(1f)
            .clip(MaterialTheme.shapes.small)
            .clickable { onSelect(tab) }
            .padding(vertical = 6.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        Icon(tab.icon, contentDescription = tab.label, tint = tint, modifier = Modifier.size(24.dp))
        Text(
            tab.label,
            style = MaterialTheme.typography.labelSmall,
            color = tint,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
        )
    }
}

@Composable
private fun AdminScopeSelector(
    scope: TradingScope,
    users: List<AdminUser>,
    onSelect: (TradingScope) -> Unit,
) {
    var expanded by remember { mutableStateOf(false) }
    Box {
        // Filled chip so the admin scope control is unmissable
        Surface(
            onClick = { expanded = true },
            shape = MaterialTheme.shapes.extraLarge,
            color = BrandTeal.copy(alpha = 0.18f),
            modifier = Modifier.padding(end = 4.dp),
        ) {
            Row(
                modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Icon(
                    Icons.Outlined.Groups,
                    contentDescription = "Admin scope",
                    tint = BrandTeal,
                    modifier = Modifier.size(16.dp),
                )
                Text(
                    "ADMIN · ${scope.label}",
                    style = MaterialTheme.typography.labelMedium,
                    fontWeight = FontWeight.Bold,
                    color = BrandTeal,
                    maxLines = 1,
                    modifier = Modifier.widthIn(max = 130.dp),
                )
                Icon(Icons.Filled.ArrowDropDown, contentDescription = null, tint = BrandTeal, modifier = Modifier.size(16.dp))
            }
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            DropdownMenuItem(
                text = { Text("All users") },
                onClick = { onSelect(TradingScope.AllUsers); expanded = false },
            )
            DropdownMenuItem(
                text = { Text("Pooled account") },
                onClick = { onSelect(TradingScope.Pool); expanded = false },
            )
            if (users.isNotEmpty()) HorizontalDivider()
            users.forEach { user ->
                DropdownMenuItem(
                    text = { Text(user.username) },
                    onClick = { onSelect(TradingScope.User(user.id, user.username)); expanded = false },
                )
            }
        }
    }
}

