package com.fallback.trading.ui.navigation

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.fallback.trading.AppContainer
import com.fallback.trading.ui.login.LoginScreen
import com.fallback.trading.ui.main.MainScreen
import com.fallback.trading.ui.server.ServerSetupScreen

object Routes {
    const val SPLASH = "splash"
    const val SERVER = "server"
    const val LOGIN = "login"
    const val MAIN = "main"
}

@Composable
fun AppNavHost(container: AppContainer) {
    val navController = rememberNavController()

    NavHost(navController = navController, startDestination = Routes.SPLASH) {
        composable(Routes.SPLASH) {
            SplashScreen(container) { destination ->
                navController.navigate(destination) {
                    popUpTo(Routes.SPLASH) { inclusive = true }
                }
            }
        }
        composable(Routes.SERVER) {
            ServerSetupScreen(
                container = container,
                onConfigured = {
                    navController.navigate(Routes.LOGIN) {
                        popUpTo(Routes.SERVER) { inclusive = true }
                    }
                },
            )
        }
        composable(Routes.LOGIN) {
            LoginScreen(
                container = container,
                onLoggedIn = {
                    navController.navigate(Routes.MAIN) { popUpTo(0) }
                },
                onChangeServer = {
                    navController.navigate(Routes.SERVER)
                },
            )
        }
        composable(Routes.MAIN) {
            MainScreen(
                container = container,
                onSignedOut = {
                    navController.navigate(Routes.LOGIN) { popUpTo(0) }
                },
            )
        }
    }
}

@Composable
private fun SplashScreen(
    container: AppContainer,
    onDecided: (String) -> Unit,
) {
    LaunchedEffect(Unit) {
        val configured = container.repository.configureFromSettings()
        val destination = when {
            !configured -> Routes.SERVER
            container.repository.resumeSession() -> Routes.MAIN
            else -> Routes.LOGIN
        }
        onDecided(destination)
    }

    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            modifier = Modifier.fillMaxSize(),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Text(
                "Fallback Trading",
                style = MaterialTheme.typography.headlineSmall,
                color = MaterialTheme.colorScheme.onBackground,
            )
            CircularProgressIndicator(Modifier.padding(top = 16.dp))
        }
    }
}
