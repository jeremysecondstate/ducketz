I want to build a HYPE perps trading system which is sort of Iron Condor inspired that utilizes our two accounts JEREMY and ALEX accounts. See screenshots.
These are my notes so far (this is a brainstorming only chat, no implementations yet!):

**JEREMY ACCOUNT**
Long Position. It's fixed. Jeremy's account is always a long position.

**ALEX ACCOUNT**
Short Position. Also fixed. Alex's account is always a short position.

**Trade Qty Fixed at 5**

**Iron Condor Inspired Calculus**
A calculus for every order placed that calculates the precise positions above and below the midprice that will generate a profit. This will take in the fee amount, funding rate, etc. any costs before the profit amount. The profit amount should be a very small fixed amount so it's position is very close to the midprice and likely to be picked up.

**Cancel Abandoned Open Orders**
Open orders that deviate too far away (a fixed amount/percentage away) from the midprice are canceled. Because we have two accounts, if price moves intensely in one direction, say up (JEREMY ACCOUNT), and our below the midprice open long-buys are abandoned, and the reduce-only short-sells are picked up (above the midprice), the short position (ALEX ACCOUNT) will be building up a larger short, while their abandoned long-buy reduce-only open orders below the midprice are also abandoned, both accounts properly readjust. If this makes sense.

**For Every Order Placed An Opposite Order Placed**
For instance: for the long position (JEREMY) we'd place a long-buy fairly close below the midprice, and a short-sell at at the nearest position price above the midprice that will generate at least a few bucks in profit. The same is done for the short position (ALEX) but obviously the mirror-image/inverse in effect.    
Available Balance
Fee Amount
