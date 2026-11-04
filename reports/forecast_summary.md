# Next-quarter rent forecast (lightgbm_v1_2026Q2_20261102)

Backtest: expanding window, last 8 quarters, one-quarter-ahead.
Published model: **lightgbm** (MAPE 1.75% vs naive 1.81%).

## Overall accuracy
    model_name  mape    mae  n_predictions
      lightgbm 1.748 12.528           5034
         naive 1.814 12.903           5034
         drift 2.203 15.595           5030
seasonal_naive 6.493 42.704           5034

## MAPE by property type
model_name   naive  seasonal_naive  drift  lightgbm
segment                                            
1 bed flat    2.10            7.64   2.58      2.08
2 bed flat    1.39            6.18   1.60      1.26
2 bed house   1.76            6.12   2.15      1.70
3 bed flat    1.91            6.57   2.31      1.80
3 bed house   1.47            5.94   1.81      1.39
4 bed house   2.30            6.58   2.81      2.30

## MAPE by region
model_name               naive  seasonal_naive  drift  lightgbm
segment                                                        
Inner Eastern Melbourne   1.87            6.86   2.23      1.77
Inner Melbourne           2.10            6.11   2.66      2.11
Mornington Peninsula      1.59            5.60   1.69      1.43
North Eastern Melbourne   1.51            6.44   1.89      1.47
North Western Melbourne   1.54            6.11   1.90      1.50
Outer Eastern Melbourne   2.07            7.64   2.25      1.82
Outer Western Melbourne   1.49            6.05   1.90      1.53
South Eastern Melbourne   1.70            7.42   2.05      1.52
Southern Melbourne        2.14            6.51   2.57      2.06

Forecast rows written: 634 for quarter ending 20260930

Note: DFFH rents are moving annual medians (leases signed in the past 12 months), so the series is
smooth and a naive forecast is hard to beat. Intervals are 80% (10th to 90th percentile).