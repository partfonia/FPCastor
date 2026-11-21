"""Список дополнительных модулей (подключается в ModuleManager)."""
from .chat import AntiSpamModule, SurveyModule, TranslateModule
from .lots import BulkPriceModule, LotTemplatesModule, WatchModule
from .money import ExportModule, GoalsModule, ReceiptsModule, RiskModule, TgBackupModule
from .sales import AutoRestoreModule, BundlesModule, LoyaltyModule, PreorderModule, StepsModule

ALL = (StepsModule, PreorderModule, BundlesModule, LoyaltyModule, AutoRestoreModule,
       TranslateModule, AntiSpamModule, SurveyModule,
       LotTemplatesModule, BulkPriceModule, WatchModule,
       GoalsModule, ExportModule, ReceiptsModule, TgBackupModule, RiskModule)
