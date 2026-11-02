# Rent drivers regression (ols_v1_20261102)

Observations: 640 suburb group x property type rows (latest 4 quarters averaged)
R-squared: 0.832   adj. R-squared: 0.828

## Distance to station (model A)
Elasticity: -0.0244 (HC3 SE 0.0059, p = 0.0000)
A 10% increase in distance is associated with a -0.23% change in rent,
holding bedrooms, dwelling type, indexed income and region constant.

## Train premium by distance band vs >5 km (model B)
  band  premium_pct  ci_low_pct  ci_high_pct  p_value
2–5 km        0.835      -3.916        5.820    0.736
1–2 km        6.611       1.436       12.050    0.012
 <1 km        8.476       3.605       13.576    0.001

## Diagnostics
VIF:
    variable  vif
log_distance 1.06
    bedrooms 1.36
    is_house 1.36
  log_income 1.06
Breusch-Pagan p-value: 0.001634 (heteroskedastic: HC3 robust SEs used)
Moran's I on suburb residuals: 0.139 (permutation p = 0.005) (residuals cluster spatially; treat SEs as optimistic, region effects already included)

## Model A summary
                            OLS Regression Results                            
==============================================================================
Dep. Variable:           np.log(rent)   R-squared:                       0.832
Model:                            OLS   Adj. R-squared:                  0.828
Method:                 Least Squares   F-statistic:                     209.1
Date:                Mon, 02 Nov 2026   Prob (F-statistic):          5.88e-210
Time:                        19:05:23   Log-Likelihood:                 394.65
No. Observations:                 640   AIC:                            -763.3
Df Residuals:                     627   BIC:                            -705.3
Df Model:                          12                                         
Covariance Type:                  HC3                                         
========================================================================================================
                                           coef    std err          z      P>|z|      [0.025      0.975]
--------------------------------------------------------------------------------------------------------
Intercept                                2.6494      0.250     10.619      0.000       2.160       3.138
C(dwelling_type)[T.House]                0.0309      0.010      3.044      0.002       0.011       0.051
C(region)[T.Inner Melbourne]             0.1954      0.019     10.516      0.000       0.159       0.232
C(region)[T.Mornington Peninsula]       -0.0567      0.022     -2.538      0.011      -0.101      -0.013
C(region)[T.North Eastern Melbourne]    -0.0991      0.020     -4.967      0.000      -0.138      -0.060
C(region)[T.North Western Melbourne]    -0.1242      0.023     -5.496      0.000      -0.168      -0.080
C(region)[T.Outer Eastern Melbourne]    -0.1226      0.017     -7.209      0.000      -0.156      -0.089
C(region)[T.Outer Western Melbourne]    -0.2054      0.021     -9.982      0.000      -0.246      -0.165
C(region)[T.South Eastern Melbourne]    -0.1621      0.024     -6.650      0.000      -0.210      -0.114
C(region)[T.Southern Melbourne]          0.0223      0.021      1.068      0.286      -0.019       0.063
np.log(distance_km)                     -0.0244      0.006     -4.117      0.000      -0.036      -0.013
bedrooms                                 0.2321      0.007     33.149      0.000       0.218       0.246
np.log(income_indexed)                   0.4213      0.032     13.136      0.000       0.358       0.484
==============================================================================
Omnibus:                       19.229   Durbin-Watson:                   1.085
Prob(Omnibus):                  0.000   Jarque-Bera (JB):               35.909
Skew:                          -0.168   Prob(JB):                     1.59e-08
Kurtosis:                       4.111   Cond. No.                         396.
==============================================================================

Notes:
[1] Standard Errors are heteroscedasticity robust (HC3)

Association, not causation: station proximity correlates with CBD access, density and amenities.