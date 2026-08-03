# UH-OPTIONS-OVERVIEW

## Page 1

Chapter 21 : Options-1 
 
CHAPTER 21 . OPTIONS 
 
Contents 
 
I.  INTRODUCTION 
• BASIC TERMS 
 
II.  VALUATION OF OPTIONS 
A.  Minimum Values of Options 
B. Maximum Values of Options 
C. Determinants of Call Value 
D.  Black-Scholes Option Pricing Model (1973, BSOPM) 
E.  Put-Call Parity 
 
III.  EXERCISE FOR PAYOFF DIAGRAM 
IV.  ASSIGNED PROBLEMS FROM THE TEXTBOOK 
V. SELF-EXERCISE PROBLEMS 
 
 
I.  INTRODUCTION 
 
• BASIC TERMS 
 
1.  Option : right to buy (or sell) an asset at a fixed price on or before a given date 
Right → buyer of option has no obligation, seller of option is obligated 
Call → right to buy 
Put → right to sell 
Note:  Option may be written on any type of asset => most common is stock 
 
2.  Exercising the option - buying or selling asset by using option 
 
3. Strike (or exercise) price - price at which asset may be bought or sold 
 
4.  Expiration date - last date on which option may be exercised 
 
5.  European option - may be exercised only at expiration date 
 
6.  American option - may be exercised on or before expiration date 
 
7.  In-the-money - positive cash flow if exercised → call [put] =?  
 
8. Out-of-the-money - negative cash flow if exercised → call [put] = ?

## Page 2

Chapter 21 : Options-2 
 
 
9. At-the-money - zero cash flow if exercised → call [put] = ?

## Page 3

Chapter 21 : Options-3 
 
 
II.  VALUATION OF OPTIONS 
 
A.  Minimum Values of Options 
 
1. Minimum Value of Call 
 
A call option is an instrument with limited liability.  If the call holder sees that it is advantageous to 
exercise it, the call will be exercised.  If exercising it will decrease the call holder's wealth, the 
holder will not exercise it.  Thus, the option cannot have negative value, because the holder cannot 
be forced to exercise it. 
 
Therefore, C ≥ 0 
 
For an American call, the statement that C ≥ 0 is dominated by a much stronger  
statement : Ca ≥ Max (0, S-E).  The expression Max (0, S-E) means  "Take the maximum value of 
the two arguments, zero or S-E" 
 
 
 
 
 
To prove this, consider the following example. 
 
Example ]  
Strike price  
= $35 
 
 
 
 
 
Stock price  
= $39 
 
• If the call were priced less than $4 -- say $3. 
You could buy the call for $3, exercise it -- which would entail buying the stock for $35 -- and    then 
sell the stock for $39.  This arbitrage transaction would give an immediate riskless profit of $1. 
 
All investors will would do this, which drives up the option price. 
 
When the price of the call reached $4, the transaction would no longer be profitable. 
THUS, $4 is the minimum price of the call. 
 
 
 
 
If stock price = $40 => min. value of call =  
If stock price = $30 => min. value of call =

## Page 4

Chapter 21 : Options-4 
 
2. Minimum Value of Put. 
Similar logic applies to put options. 
 
 
P ≥ 0 
For an American put, the statement that P ≥ 0 is dominated by a much stronger 
statement : Pa ≥ Max (0, E-S).  The expression Max (0, E-S) means  "Take the 
maximum value of the two arguments, zero or E-S" 
 
 
 
If stock value = $40 => min. value =  
If stock value = $30 => min. value =  
 
 
B. Maximum Values of Options 
 
1. Maximum Value of Call 
Ca ≥ Max (0, S-E) 
From the equation above, even if the exercise price were zero, no one would pay more for the 
call than for the stock.  Therefore, the maximum value of a call is the stock price. 
 
2. Maximum Value of Call 
Pa ≥ Max (0, E-S) 
From the equation above, even if the exercise price were zero, no one would pay more for the 
put than for the stock.  Therefore, the maximum value of a put is the stock price.

## Page 5

Chapter 21 : Options-5

## Page 6

Chapter 21 : Options-6 
 
C. Determinants of Option Value 
 
 
There are six factors affecting the price of a stock options. 
1. The current stock price 
2. The exercise [or strike] price 
3. The time to expiration 
4. The volatility of the stock price 
5. The risk free interest rate 
6. The dividends expected during the life of the option 
 
 
: In this section, we consider what happens to option prices when one of tehse factors changes 
with all the others remaining fixed. 
 
1. Current stock price 
→ If it is exercised at some time in the future, the payoff from a call option will be the  
amount by which stock price exceeds the strike price.  Therefore call option becomes  
more valuable as the stock price increases. 
2.  Exercise price 
→ If it is exercised at some time in the future, the payoff from a call option will be the  
amount by which stock price exceeds the strike price.  Therefore call option becomes  
less valuable the strike price increases. 
3.  Time to expiration 
→ Both put and call American options become more valuable as the time to expiration increases.  
To see this, consider two options that differ only as far as the expiration date is concerned.  The 
owner of the long-life option has all the exercise opportunities open to the owner of the short-life 
option -- and more.  The long-life option must therefore always be worth at least as much as the 
short-life option. 
European put and call options do not necessarily become more valuable as the time to expiration 
increases. [why?] 
4. Volatility [or Variance ] in stock prices 
→ Roughly speaking, the volatility of a stock price is a measure of how uncertain we 
are about future stock price movements. 
As volatility increases, the chance that the stock will do very well or very poorly increases.  The 
owner of a call benefits from price increases but has limited downside risk in the event of price 
decreases.  [why?] Therefore value of calls increases as volatility increases. 
 
 
The same logic applies to put options. 
5. Risk free interest 
→ If the stock price is expected to increase, an investor can choose to either buy the stock or buy 
the call.  Purchasing the call will cost far less than purchasing the stock.  The difference can be 
invested in risk-free bonds.  If interest increase, the combination of calls and risk-free bonds will 
be more attractive.  This means that the call price will tend to increase with increases in interest 
rates.  However, when you sell the stock by exercising the put, you receive E dollars.  If interest 
rates increase, the E dollars will have a lower present value.  Thus, higher interest rates make put 
less attractive.

## Page 7

Chapter 21 : Options-7

## Page 8

Chapter 21 : Options-8 
 
 
• Summary of the Effect on the Option Price of Increasing one variable holding other 
variables constant 
 
 
 
 
 
 
 
 
E.Call  
E.Put  
A.Call  
A.Put 
Stock price  
 
 
 
+  
 
 
-  
 
 
+  
 
 
- 
Exercise price  
 
 
-  
 
 
+  
 
 
-  
 
 
+ 
Time to expiration  
?  
 
 
?  
 
 
+  
 
 
+ 
Volatility [Variance] +  
 
 
+  
 
 
+  
 
 
+ 
Risk-Free Rate  
 
 
+  
 
 
-  
 
 
+  
 
 
-

## Page 9

Chapter 21 : Options-9 
 
D.  Black-Scholes Option Pricing Model (1973, BSOPM) 
 
1.  Assumptions 
 
(1)  No dividends (or other distributions) 
(2)  No transaction costs or taxes 
(3)  There is a constant risk free rate of interest at which people can borrow and lend any amount of 
money 
(4) Unrestricted short selling of stocks is possible 
(5) All option are European 
(6)  Security trading is continuous 
(7) Stock price movements are random & continuous (no jumps) 
(8) Stock returns are normally distributed 
 
Note:  Few of these assumptions are reasonable, but it works. 
 
2.  The model 
 
C0 = S0[N(d1)] - E * e r t
f
−
 [N(d2)] 
 
d1 = 
ln S
E
r
t
t
f
0
2
2
2



+
+






×
σ
σ
 
 
d2 = d1 - σ2 × t   
 
C0 = value of call today 
S0= current stock price 
E = exercise price of call 
e = exponential function ≈ 2.7183 
rf = continuously compounded annual risk free rate (decimal) 
Note:  APR 
σ2 = variance of continuously compounded annual return on stock (decimal) 
t = time to expiration on annual basis 
N(d1); N(d2) = value of the cumulative normal distribution of d1 & d2 respectively 
 
Note:  All variables are observable (in WSJ) except for σ2 
rf =>  
 
σ2 =>

## Page 10

Chapter 21 : Options-10 
 
3.  Using the BSOPM 
 
(1) Some practice using N( ) 
→ see Normal Distribution Tables 
 
Ex. d1 = 0 → at mean 
=> N(d1) = ? 
 
(2)  Putting it all together 
 
Ex.  You are considering purchasing a call option on Gurgle Baby Food Inc.  This option has a 
strike price of $55 and expires 65 days from today.  The return on a 65-day T-bill is 3.5% 
per year compounded continuously.  Gurgle’s current stock price is $58 per share and the 
standard deviation of returns on Gurgle is 46%.  What is the value of this call option? 
 
σ2 =  
t = 65/365 
d1 = 
N(d1) =  
N(d2) =  
→ C0 =

## Page 11

Chapter 21 : Options-11 
 
 
E.  Put-Call Parity 
 
1.  Payoff for purchasing a put 
 
 
2.  Artificial Put 
 → Buying a call, short selling stock, and lending the present value of the exercise price provides 
same payoff as buying a put. 
 
(1) Buying call 
 
 
(2)  Short selling stock 
 
 
(3)  Lending the present value of the exercise price

## Page 12

Chapter 21 : Options-12 
 
 
(4)  Buy call, short sell stock, and lend the present value of the exercise price 
 
 
 
3.  Using Put-Call Parity to value puts 
 
→ Payoff from buying put =  
 
Payoff from buying call  
+  Payoff from short selling stock,  
+  Payoff from lending the present value of the exercise price 
 
→ P0 = C0 - S0 +  E * e-rf ×t  
 
Example ) Option has a strike price of $55 and expires 65 days from today.  The return on a 65-day 
T-bill is 3.5% per year compounded continuously.  Gurgle’s current stock price is $58 per share 
and the standard deviation of returns on Gurgle is 46%.  What is the value of this put option? 
 
Note:  The call on Gurgle Baby Food is worth $6.14 (figured earlier) 
→  P0 = ?

## Page 13

Chapter 21 : Options-13 
 
III. EXERCISE FOR THE PAYOFF DIAGRAMS. 
 
A. CALL OPTION 
 
A call option is a contract giving its owner the right [Not the obligation] to buy a fixed amount of a specified 
underlying asset at a fixed price at any time or on or before a fixed date. For example, for an equity option, the 
underlying asset is the common stock. The fixed price is called the exercise price or the strike price. The fixed date 
is called the expiration date. On the expiration date, the value of a call on a per share basis will be the larger of the 
stock price minus the exercise price or zero.  
 
a.. Long Call Option 
One can think of the buyer of the option paying a premium (price) for the option to buy a specified quantity at a 
specified price any time prior to the maturity of the option. Consider an example. Suppose you buy an option to buy 1 
UH stock at a price of $76. The option can be exercised on Dec. 19th.   The cost of the call is assumed to be $1. 
Let's tabulate the payoffs at expiration.  
Consider the payoffs diagrammatically. Notice that the payoffs are one to one after the price of the 
underlying security rises above the exercise price. When the security price is less than the exercice price, the call 
option is referred to as out of the money.  
 
                                    
Payoff for Buying Call Option
 : Exercise price : $76
-2
-1
0
1
2
3
4
70
71
72
73
74
75
76
77
78
79
80
Stock Price
Payoff
       LONG CALL OPTION PAYOFF
UH stock price
Payoff
on Dec. 19
on Option
71
-1
72
-1
73
-1
74
-1
75
-1
76
-1
77
0
78
1
79
2
80
3

## Page 14

Chapter 21 : Options-14 
 
 
b. Short Call Option 
 
Writing or "shorting" options have the exact opposite payoffs as purchased options.  
The payoff table for the short call option is: 
 
 
 
 
Notice that the liability is potentially unlimited when you are writing call options.  
 
 
                                   
 
Payoff for Selling Call Option
 : Exercise price : $76
-4
-3
-2
-1
0
1
2
70
71
72
73
74
75
76
77
78
79
80
Stock Price
Payoff
       SHORT CALL OPTION PAYOFF
UH stock price
Payoff
on Dec. 19
on Option
71
1
72
1
73
1
74
1
75
1
76
1
77
0
78
-1
79
-2
80
-3

## Page 15

Chapter 21 : Options-15 
 
 
B. PUT OPTION 
 
A put option is a contact giving its owner the right to sell a fixed amount of a specified underlying asset at a fixed 
price at any time on or before a fixed date. On the expiration date, the value of the put on a per share basis will be 
the larger of the exercise price minus the stock price or zero.  
 
 
A. Long Put Option 
One can think of the buyer of the put option as paying a premium (price) for the option to sell a specified 
quantity at a specified price any time prior to the maturity of the option. Consider an example of a put on the same 
UH stock. The exercise price is $76. You can exercise the option on Dec. 19.  
Suppose that the cost of the put is $1.00.  
 
 
 
The payoff from a long put can be illustrated. Notice that the payoffs are one to one when the price of the security 
is less than the exercise price.  
 
 
Payoff for Buying Put Option
 : Exercise price : $76
-2
-1
0
1
2
3
4
5
70
71
72
73
74
75
76
77
78
79
80
Stock Price
Payoff
       LONG PUT OPTION PAYOFF
UH stock price
Payoff
on Dec. 19
on Option
71
4
72
3
73
2
74
1
75
0
76
-1
77
-1
78
-1
79
-1
80
-1

## Page 16

Chapter 21 : Options-16 
 
B. Short Put Option 
 
Writing or "shorting" options have the exact opposite payoffs as purchased options.  
The payoff table for the short put option is: 
 
 
The payoff from a short put can be illustrated.  
 
 
 
IV. 
ASSIGNED PROBLEMS FROM THE TEXTBOOK 
Q1 – Q13 
 
Payoff for Selling Put Option
 : Exercise price : $76
-5
-4
-3
-2
-1
0
1
2
70
71
72
73
74
75
76
77
78
79
80
Stock Price
Payoff
       SHORT PUT OPTION PAYOFF
UH stock price
Payoff
on Dec. 19
on Option
71
-4
72
-3
73
-2
74
-1
75
0
76
1
77
1
78
1
79
1
80
1

## Page 17

Chapter 21 : Options-17 
 
 
V. SELF-EXERCISE PROBLEMS 
 
Show the payoff diagram and table under the following conditions. 
[ Keep in mind that Short position = Selling    and     Long position = Buying ] 
 
For Question 1) and Question 2) only 
• The call and put price are $1.   
• The exercise prices for call and put option are $76 
• You bought / or sold short a stock at $76.   
Question 1) 
Long position in a stock + Short position in a call 
Question 2) 
Short position in a stock + Short position in a put 
 
 
Question 3) 
Long position in a call with a exercise price of $73 and call price of $3 
Short position in a call with a exercise price of  $76  and call price of $1 
 
 
Question 4) 
Long position in a call and Long position in a put. 
- 
Each with exercise price of $76 and option price of $1. 
 
Question 5) 
Short position in a call and Short position in a put. 
- 
Each with exercise price of $76 and option price of $1. 
 
Question 6) 
- 
Long position in a call with exercise price of $71 and option price of $10. 
- 
Long position in a call with exercise price of $81 and option price of $5 
- 
Short position in two calls with exercise price of $76 and option price of $7. 
 
Question 7) 
- 
Long position in a put with a exercise price of $70 and option price of $2 
- 
Long position in a call with a exercise price of $76 and option price of $1 
 
Question 8) 
- 
Short position in a put with a exercise price of $70 and option price of $2 
- 
Short position in a call with a exercise price of $76 and option price of $1
