# NYU-ML-FINVAL

## Page 1

Predicting Future Earnings Changes Using  
Machine Learning and Detailed Financial Data 
 
 
Xi Chen 
xc13@stern.nyu.edu 
Department of Technology, Operations, and Statistics 
 
Yang Ha (Tony) Cho 
yhc479@stern.nyu.edu 
Department of Accounting  
 
Yiwei Dou 
yd18@stern.nyu.edu 
Department of Accounting  
 
Baruch Lev 
bil1@stern.nyu.edu 
Department of Accounting 
 
Stern School of Business 
New York University 
 
 
February 2022 
 
 
Journal of Accounting Research, Forthcoming 
 
 
 
Accepted by Christian Leuz. An earlier version of this paper circulated under the title 
“Fundamental Analysis of Detailed Financial Data: A Machine Learning Approach.” We 
benefited from the comments of an anonymous reviewer, an anonymous associate editor, 
Aleksander Aleszczyk, Karthik Balakrishnan, Jeremy Bertomeu, Oliver Binz, Elizabeth 
Blankespoor, Mark Bradshaw, John Core, Robert Holthausen, Amy Hutton, Charles M.C. Lee, 
E. Jin Lee, Becky Lester, Miao Liu, Joshua Livnat, Michael Minnis, Miguel Minutti-Meza, 
Joseph Piotroski, K. Ramesh, Joshua Ronen, Christine Tan, Daniel Taylor, Siew Hong Teoh, 
Chenqi Zhu, participants at the 2021 Journal of Accounting Research Conference and the 2021 
Transatlantic Doctoral Conference, and seminar participants at Boston College, Florida 
International University, New York University, Rice University, UC Irvine, and Stanford 
University. An Online Appendix to this paper can be downloaded 
at http://research.chicagobooth.edu/arc/journal-of-accounting-research/online-supplements
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 2

Predicting Future Earnings Changes Using  
Machine Learning and Detailed Financial Data 
 
 
 
 
 
ABSTRACT 
We use machine learning methods and high-dimensional detailed financial data to predict the 
direction of one-year-ahead earnings changes. Our models show significant out-of-sample 
predictive power: the area under the Receiver Operating Characteristics curve (AUC) ranges 
from 67.52 to 68.66 percent, significantly higher than the 50 percent of a random guess. The 
annual size-adjusted returns to hedge portfolios formed based on the prediction of our models 
range from 5.02 to 9.74 percent. Our models outperform two conventional models that use 
logistic regressions and small sets of accounting variables, and professional analysts’ forecasts. 
Analyses suggest that the outperformance relative to the conventional models stems from both 
nonlinear predictor interactions missed by regressions and the use of more detailed financial data 
by machine learning. 
 
JEL codes: C53; G12; G17; M41 
Keywords: Direction of Earnings Changes; Prediction; Detailed Financial Data; XBRL; Machine 
Learning 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 3

1 
 
1. Introduction 
Developing corporate earnings prediction models is of significant importance to 
accounting researchers and investment practitioners. However, future earnings are difficult to 
forecast as they are related to numerous aspects of a company in a complex manner with little 
guidance from theoretical literature (Lev and Gu [2016]; Monahan [2018]). Previous studies 
often use a small set of financial predictors and regression models. The former is unlikely to 
capture the high dimensional aspects relevant to future earnings; the latter cannot approximate 
the complex relations. To push the frontier of earnings prediction, we apply machine learning 
methods to a large set of detailed financial data to predict the direction of one-year-ahead 
earnings changes. We seek to investigate (1) the out-of-sample performance of our models and 
(2) performance differences between our models and conventional models as well as analysts’ 
forecasts. 
We examine the direction of earnings changes for several reasons. First, it is difficult to 
predict the level of future earnings and the amount of earnings changes (future earnings minus 
known current earnings), as extant studies find that earnings forecasts based on firm 
characteristics are not substantially more accurate than forecasts obtained from the random-walk 
model (Gerakos and Gramacy [2013]; Li and Mohanram [2014]). Second, Freeman et al. [1982, 
643] argue that the variability in earnings changes is too large to be compared to the variability 
in expected earnings changes conditional on explanatory variables. They propose to reduce the 
variability in earnings changes by transforming the amount to the direction of earnings changes, 
predicting which is more achievable. Third, forecasting the sign of earnings changes is 
economically meaningful and actionable as extensive research constructs portfolios based on the 
direction of earnings changes (Ou and Penman [1989]; Wahlen and Wieland [2011]). 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 4

2 
 
We use two widely accepted machine learning methods based on decision trees: random 
forests and stochastic gradient boosting, which have recently achieved remarkable success in 
real-world applications (Zhou [2012]; Mullainathan and Spiess [2017]; Liu [2021]). Compared 
with regressions, these methods have three advantages. First, they can accommodate a far more 
expansive list of predictors to utilize more nuanced information in detailed financial data. For 
example, we can estimate machine learning models when the number of predictors is even 
greater than the number of observations, whereas traditional regressions break down for such a 
scenario. Second, the machine learning algorithms cast a wide net in their specification search to 
allow complex associations between high-dimensional predictors and the predicted variable. 
Third, these algorithms are specialized for prediction tasks, rather than explanation tasks. They 
offer high out-of-sample predictive performance by using the “regularization” (e.g., using a 
number of decision trees in random forests) to mitigate overfitting. 
To obtain detailed financial data in a machine-readable format, we use financial reports 
filed in eXtensible Business Reporting Language (XBRL). XBRL is an extensible markup 
language comprised of a standard list of tags (“taxonomy”) to describe business and financial 
information. Since 2012, all U.S. public companies must have XBRL tags on quantitative 
amounts in financial statements and footnotes of their 10-K reports. Commercial data 
aggregators have very limited coverage of these XBRL-tagged detailed financial data, 
particularly for footnote disclosures. 
Our sample is comprised of over 8,000 XBRL filings from 2012 to 2018. These filings 
contain more than 4,000 distinct financial items in standard tags common throughout our sample 
period. We take all the items for the current and lagged years, divide them by total assets, and 
compute the annual percentage changes, which yield over 12,000 explanatory variables (i.e., 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 5

3 
 
4,000 × 3 for current values, lagged values, and percentage changes). For each year in the test 
period, 2015 to 2018, we use the second and third preceding years as the machine learning 
training period to estimate models, and the preceding year as the validation period to select the 
model that yields the best out-of-sample performance. The chosen model is then applied to the 
year in the test period to produce the summary measure Pr, which characterizes the probability 
of an increase in the next year’s earnings. 
To evaluate model performance, we use the area under the Receiver Operating 
Characteristics (ROC) curve (AUC) and 12-month size-adjusted excess returns to the hedge 
portfolios formed three months after the fiscal-year end based on Pr in the test period. While 
AUC is commonly used in classification problems, the excess returns offer an economic meaning 
for the prediction gains.1 We find significant out-of-sample predictability of our models using 
machine learning and detailed financial data, concerning the direction of the next year’s earnings 
changes. The AUC in the test period ranges from 67.52 to 68.66 percent, significantly higher 
than the 50 percent of a random guess. The annual size-adjusted returns to the hedge portfolios 
are both economically and statistically significant, ranging from 5.02 to 9.74 percent. 
We compare our models with three benchmarks. First, following Ou and Penman [1989], 
we estimate logistic regressions using their 65 financial variables, which represent a “kitchen 
sink” approach with a large number of predictors.2 Our models significantly outperform Ou and 
Penman’s [1989], which exhibits an AUC of only 61.79 percent and annual size-adjusted returns 
 
1 Holthausen and Larcker [1992] use logistic regressions and the same set of financial variables as Ou and Penman 
[1989] to directly predict the sign of future stock returns. Recent studies also apply machine learning to small sets of 
variables to directly predict future stock returns (Chinco et al. [2019]; Rasekhschaffe and Jones [2019]; Livnat and 
Singh [2021]). How to leverage machine learning methods to analyze a large set of detailed financial data in XBRL 
documents for direct return predictions presents an opportunity for future research. 
2 Ou and Penman [1989] won the 1991 AAA Notable Contributions to Accounting Literature Award and was 
identified as the 11th most cited article during 1976-1993 by Brown [1996]. It was cited by 1575 (302) articles on 
Google Scholar (Web of Science) as of January 10, 2022. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 6

4 
 
of 2.48 percent in the test period. We investigate the source of this superior performance by 
applying the same machine learning methods to Ou and Penman’s 65 financial variables. These 
hybrid models exhibit an AUC of 66.63 to 66.87 percent and annual size-adjusted returns of 3.97 
to 4.67 percent. They significantly outperform the original Ou and Penman’s regression model 
and marginally underperform those we build using both machine learning and detailed financial 
data. The results suggest that our models’ superior performance relative to Ou and Penman’s 
[1989] stems primarily from nonlinear predictor interactions in machine learning, which are 
missed by regressions, and secondarily from the use of more detailed financial information. 
Second, we estimate a logistic regression using variables from the DuPont decomposition 
by Nissim and Penman [2001]. These variables are identified by experts with an internal 
structure and could improve predictive performance. However, forecasts from this model exhibit 
an AUC of 57.96 percent and annual size-adjusted returns of 1.90 percent in the test period, still 
significantly lower than those of our models. Applying our machine learning methods to the 
DuPont variables yields an AUC of 61.15 to 61.51 percent and annual size-adjusted returns of 
2.12 to 2.73 percent, higher than the original logistic model with the DuPont variables but lower 
than the models using both machine learning and detailed financial data. The results suggest that 
both nonlinear predictor interactions and more detailed financial information than the DuPont 
variables contribute to our models’ outperformance. 
Third, we compare our models with analysts’ forecasts issued in the month following the 
portfolio formation when all the detailed financial information in 10-Ks is available and can be 
incorporated by these professionals. Our models significantly outperform analysts’ earnings 
forecasts, which exhibit an AUC of 64.71 percent and annual size-adjusted returns of 3.93 
percent in the test period. The results highlight the usefulness of machine learning and detailed 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 7

5 
 
financial information in earnings prediction, even in the presence of professional forecasters, 
who may have limited ability to process detailed financial data. 
While XBRL documents offer detailed financial data, there are several reasons the data 
quality could be compromised, affecting our predictions. First, XBRL documents are not audited, 
and errors in these documents are subject to only modified liability within two years after the 
initial adoption (a safe harbor provision in Rule 406T; SEC [2009]). Second, firms can choose to 
use a custom tag called an “extension” rather than a standard tag. Research finds errors and 
unnecessary extensions in XBRL documents, as semantically equivalent tags already exist in the 
taxonomy, particularly in early adoption years (Debreceny et al. [2010, 2011]). Third, in addition 
to custom tags, firms also combine tags with dimensions to report information by category (e.g., 
for segment reporting). Regardless of whether custom tags and tags with dimensions are 
necessary, these items are typically firm-specific and thus not comparable across firms in a large 
sample. As a result, we are unable to use them in our models. 
We conduct two sets of analyses to examine whether data quality issues temper the 
usefulness of detailed financial data in XBRL documents. First, we use Compustat as an 
alternative source of detailed financial information. Compared with XBRL-tagged financial data, 
Compustat has the advantage of more extensive standardized adjustments to improve data quality 
and the disadvantage of less detailed coverage of financial information (e.g., footnote 
disclosures). Using the machine learning methods, we continue to find robust predictive power 
for the Compustat data (with an AUC of 67.39 to 69.40 percent and annual size-adjusted returns 
of 3.95 to 10.07 percent), similar to XBRL-tagged data. Thus, the benefit of additional financial 
details relative to Compustat is offset by the presence of errors, custom tags, and tags with 
dimensions in XBRL documents. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 8

6 
 
Second, we find better predictive performance in more recent years than in the early 
period, likely due to low-quality XBRL-tagged financial data in the early years. We also partition 
the sample based on a firm-level measure of the prevalence of tags that cannot be used in our 
large-sample analysis and observe worse predictive performance in the subsample with more 
such tags. The results suggest that errors and the prevalence of tags that cannot be used in a large 
sample reduce the usefulness of detailed financial data in XBRL documents. 
Finally, to provide more transparency on the inner workings of our models, we estimate 
each variable’s importance by computing the decrease in the predictive performance when that 
variable is randomly shuffled (Breiman [2001]). The majority of the top 10 most important 
variables pertain to earnings components (e.g., operating income and earnings per share), 
suggesting that current earnings are still leading indicators among financial numbers for future 
earnings. We also classify the variables into six groups (the five financial statements and 
footnotes) and find that in aggregate, footnote disclosures contribute most to our models’ 
predictive power, followed by the balance sheet, income statement, and cash flow statement. 
Comprehensive income statement and shareholders’ equity statement contribute the least. 
Among footnote disclosures, the list of top 10 most important variables is dominated by tax-
related items (e.g., valuation allowance for deferred tax assets), consistent with tax items 
carrying important information about future taxable income (Miller and Skinner [1998]; Lev and 
Nissim [2004]; Hanlon [2005]; Thomas and Zhang [2011]). We also observe meaningful 
nonlinear and interaction effects of predictors. 
Our study makes three contributions. First and foremost, we contribute to the earnings 
prediction literature by applying machine learning algorithms to a large set of detailed financial 
data. Several concurrent papers employ machine learning methods to forecast future earnings 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 9

7 
 
using a small group of financial statement variables (Gerakos and Gramacy [2013]; Anand et al. 
[2019]; Hunt et al. [2019]; Cao and You [2020]; Binz et al. [2021]). Our detailed financial data 
offer more nuanced information than the small set of variables and unleash the power of machine 
learning, which can accommodate a far more expansive list of predictors in a non-linear fashion. 
Second, an emerging line of research uses machine learning algorithms in accounting and 
finance research. Several studies use these algorithms to detect accounting fraud or restatements 
(Cecchini et al. [2010]; Perols [2011]; Bao et al. [2020]; Bertomeu et al. [2021]). Barth et al. 
[2021] examine the value relevance of accounting numbers using decision trees. Ding et al. 
[2020] employ machine learning to improve reserve estimates in the insurance industry. 
Researchers also apply machine learning to refine the measurement of expected stock returns 
(Freyberger et al. [2020]; Gu et al. [2020]) and to extract information from 10-K textual 
disclosures (Li [2010]; Frankel et al. [2016]; Dyer et al. [2017]; Cohen et al. [2020]). We 
demonstrate that machine learning can help advance one of the most widely studied areas in 
research and practice—earnings prediction using detailed financial information. 
Finally, we add to the XBRL literature. SEC [2009] commented that the XBRL format of 
financial reports could “improve its usefulness to investors. In this format, financial statement 
information could be downloaded directly into spreadsheets, analyzed in a variety of ways using 
commercial off-the-shelf software, and used within investment models in other software 
formats.” Despite the stated goal, the usefulness of XBRL-tagged financial data to investors 
remains an open question that is important to regulators, practitioners, and academics.3 Previous 
 
3 Richardson et al. [2010, 446] call for more research on the usefulness of XBRL-tagged financial data: “The 
development and US adoption of eXtensible Business Reporting Language (XBRL)… means that users now have 
substantially more information in machine readable form to conduct large-scale archival analyses for the usefulness 
of that information for forecasting purposes. The set of information contained in financial reports is too detailed to 
list, but we expect to see research efforts utilizing this information to be worthwhile.” 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 10

8 
 
studies find that the adoption of XBRL influences capital market outcomes (Blankespoor et al. 
[2014]; Dong et al. [2016]; Bhattacharya et al. [2018]; Kim et al. [2019a]) and corporate 
reporting decisions (Blankespoor [2019]; Kim et al. [2019b]). These studies assume that XBRL-
tagged financial data contain useful fundamental signals for investors. This assumption, 
however, is challenged by research documenting errors and unnecessary extensions in XBRL 
filings (Debreceny et al. [2010], [2011]) and the associated adverse consequences in the capital 
markets (Li and Nwaeze [2015], [2018]; Kirk et al. [2016]). Our paper provides direct evidence 
indicating that XBRL filings still inform investors’ forecasts and investment decisions despite 
the data quality issues. 
 
2. Background 
2.1. MACHINE LEARNING USING DECISION TREES 
 
We use two widely accepted machine learning methods based on decision trees. Decision 
trees are a popular statistical learning approach for incorporating nonlinearities and interactions. 
Unlike regressions, decision trees are built nonparametrically and designed to group observations 
with similar predictors. The tree “grows” in a sequence of steps. At each step, the sample 
leftover from the preceding step is split based on one predictor variable. Typically, the algorithm 
will try every possible cutoff for each predictor and choose the split that minimizes forecast 
errors (“impurity”) before the next step. The split stops when a further partition cannot reduce 
forecast errors, or a tree attribute (e.g., tree depth 𝐿𝐿 or the minimum number of elements in a 
group 𝑏𝑏) reaches a prespecified threshold (i.e., an early stopping criterion) that can be selected 
adaptively using a validation sample (Hastie et al. [2009]; Varian [2014]). 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 11

9 
 
The left panel of Figure 1 presents an example of a decision tree to forecast a one-year-
ahead earnings increase. Suppose the tree has two predictors, “EPS” and “Lev” (i.e., earnings per 
share and leverage calculated as liabilities divided by total assets), and the associated threshold is 
the final output based on the training sample. The tree describes how each observation is 
assigned to a group based on its predictor value. A blue box (“a node”) represents a split, and a 
green box (“a leaf”) indicates a final partition. First, the sample is sorted on EPS. Observations 
with EPS above the breakpoint of 0.5 are assigned to Group 1. Those with EPS at or below 0.5 
are then further sorted by Lev: observations with Lev below 0.7 go into Group 2, while those 
with Lev at or above 0.7 are assigned to Group 3. The right panel of Figure 1 shows how the 
space of “EPS” and “Lev” is partitioned by this tree model. The mode of the one-year-ahead 
earnings increase indicator for each group (“the majority vote rule”) from the training sample 
forms the binary forecast (i.e., 𝑦𝑦ො= 1 for an earnings increase and 0 otherwise) for a new 
observation (from a validation sample or a test sample) that is assigned to that group based on its 
predictor value. We can recast the forecasts of the tree as a linear function: 𝑦𝑦ො= 𝛽𝛽11{𝐸𝐸𝐸𝐸𝐸𝐸>0.5} +
𝛽𝛽21{𝐸𝐸𝐸𝐸𝐸𝐸≤0.5}1{𝐿𝐿𝐿𝐿𝐿𝐿<0.7} + 𝛽𝛽31{𝐸𝐸𝐸𝐸𝐸𝐸≤0.5}1{𝐿𝐿𝐿𝐿𝐿𝐿≥0.7}, where 𝛽𝛽𝑖𝑖 denotes the mode of the earnings 
increase indicator for group 𝑖𝑖 in the training sample and 1{.} is set to one when the curly bracket 
statement is true, and zero otherwise.4 
A decision tree has four advantages. First, while considering all explanatory variables, it 
uses only one predictor for each split and generates forecasts nonparametrically. As a result, 
there is no need to require a sufficient number of observations relative to the number of 
predictors, as is necessary for traditional regression analysis. Second, a decision tree is invariant 
 
4 This example illustrates how to form a forecast of an earnings increase using a single decision tree model with a 
zero-one loss function. Nevertheless, we do not use the single decision tree model for our subsequent analyses. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 12

10 
 
to monotonic transformations of predictors. Third, a decision tree can approximate a high degree 
of nonlinearities. Fourth, a decision tree of depth 𝐿𝐿 allows 𝐿𝐿−1-way interactions. The 
flexibility, however, also makes decision trees prone to overfit and thus calls for regularization. 
We consider two tree regularizers: random forests and stochastic gradient boosting. Both 
combine forecasts from many different trees into a single forecast (an “ensemble learning” 
approach). 
Random forests use two procedures to regularize decision trees. First, in the bootstrap 
aggregation procedure, also known as “bagging” (Breiman [2001]), a tree is grown based on 
each of 𝑚𝑚 different bootstrap samples of the training data, as shown in Figure 2. Once a random 
forests model is developed, each new observation (from a validation sample or a test sample) 
generates 𝑚𝑚 predictions (𝑦𝑦ො1 , 𝑦𝑦ො2 , …, 𝑦𝑦ො𝑚𝑚), with the final forecast being a simple average of them 
(𝑃𝑃𝑃𝑃
෢). Trees tend to overfit the individual bootstrap samples, which makes their individual 
predictions less effective. Averaging over 𝑚𝑚 predictions reduces this ineffectiveness (i.e., 
variance in the predicted model) and enhances the predictive performance. Second, if there is a 
dominant predictor in the data, then most of the bagged trees will split on this predictor at a low 
level, leading to a significant correlation among their ultimate forecasts. The “dropout” 
procedure decorrelates trees by considering only a random subset of predictors (𝑘𝑘 variables) for 
each split. As a result, the dominant predictor may not be considered for some splits. The 
decreased correlation among predictors can further improve the variance reduction and mitigate 
the issue of overfitting. 
Unlike random forests, which grow trees independently, stochastic gradient boosting 
builds a tree based on the previous tree’s forecast errors (“boosting”), as shown in Figure 3. To 
develop a stochastic gradient boosting model using a training sample, we start by computing the 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 13

11 
 
initial log-odds 𝐹𝐹0(𝑥𝑥) = log ቀ
#𝑜𝑜𝑜𝑜 𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼
#𝑜𝑜𝑜𝑜 𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷ቁ, which can be converted to the initial prediction, 
𝑃𝑃𝑃𝑃0
෢=
𝑒𝑒𝐹𝐹0(𝑥𝑥)
1+𝑒𝑒𝐹𝐹0(𝑥𝑥). We then fit a shallow tree (e.g., with depth 𝐿𝐿 = 1) to the residuals from the initial 
prediction, 𝑟𝑟0 = 𝑦𝑦−𝑃𝑃𝑃𝑃0
෢, where 𝑦𝑦= 1 for an earnings increase and 0 otherwise. The residuals 
are converted to 𝛾𝛾0ෝ= 
∑𝑟𝑟0
∑[𝑃𝑃𝑃𝑃0
෢×(1−𝑃𝑃𝑃𝑃0
෢)], where the summation is for each leaf, shrunken by a factor 
𝜌𝜌∈(0,1) (i.e., the learning rate), and added to the initial log-odds to form an updated log-odds 
𝐹𝐹1(𝑥𝑥) = 𝐹𝐹0(𝑥𝑥) +  𝜌𝜌× 𝛾𝛾0ෝ and an updated prediction 𝑃𝑃𝑃𝑃1
෢=
𝑒𝑒𝐹𝐹1(𝑥𝑥)
1+𝑒𝑒𝐹𝐹1(𝑥𝑥). Then the next tree with the 
same shallow depth 𝐿𝐿 is used to fit the residuals from the previous prediction. This is repeated 𝑚𝑚 
times. The “stochastic” procedure uses a random sample in each iteration to decorrelate estimates 
at different iterations. Friedman [2002] shows that this procedure effectively reduces the 
variance of the combined model. Once a stochastic gradient boosting model is developed using a 
training sample, for a new observation (from a validation sample or a test sample), the output of 
this additive model of shallow trees yields a series of predictions (e.g., 𝑃𝑃𝑃𝑃0
෢, 𝑃𝑃𝑃𝑃1
෢, 𝑃𝑃𝑃𝑃2
෢) and the 
final ensemble prediction 𝑃𝑃𝑃𝑃
෢=
𝑒𝑒𝐹𝐹𝑚𝑚(𝑥𝑥)
1+𝑒𝑒𝐹𝐹𝑚𝑚(𝑥𝑥).5 
2.2. DETAILED FINANCIAL ACCOUNTING DATA IN XBRL 
Although listed companies’ financial reports are publicly available, arranging the detailed 
financial information in a machine-readable format for large-scale analysis is non-trivial. While 
commercial data aggregators (e.g., Yahoo Finance and Compustat) collect many financial 
statement items, their coverage of footnote disclosures is very limited. To overcome this 
challenge, we take advantage of financial reports filed in XBRL. 
 
5 For technical details of the two machine learning algorithms, see Breiman [2001] and Hastie et al. [2009, Chapter 
12] for random forests and Friedman [2002] and Hastie et al. [2009, Chapter 10] for stochastic gradient boosting. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 14

12 
 
The SEC mandate of 2009 (“Interactive Data to Improve Financial Reporting”) required 
public companies to provide their financial reports in XBRL to the SEC and post them on their 
corporate websites.6 The XBRL format disclosure is in addition to disclosure in the traditional 
electronic filing formats of ASCII or HTML. It provides a means to convert the information from 
human-readable formats (e.g., paper, PDF, and HTML) to a machine-readable format, 
comparable to the shift from paper maps to digital maps. The requirements begin for the first 
quarterly report for a period ending after a) June 15, 2009, for large accelerated filers (with a 
public equity float over $5 billion), b) June 15, 2010, for other large accelerated filers (with a 
public equity float over $700 million), and c) June 15, 2011, for all remaining filers. In the first 
year of XBRL filings, companies must tag each quantitative item on the face of financial 
statements and each footnote as a block. In the subsequent filing years, companies must also tag 
the detailed quantitative disclosures within the footnotes.7 Accordingly, 2012 is the earliest year 
with available XBRL-tagged financial statement and footnote items for all firms. The mandate 
requires filers to completely align their XBRL report to the traditional ASCII or HTML report 
(SEC [2009]). As a result, a restated financial statement (due to errors or changes in reporting 
practices) does not change the original XBRL document. It will be reported in a subsequent 
filing (e.g., a 10-K/A), for which there is another XBRL document. This point-in-time feature 
avoids issues related to data backfilling in capital market research. 
 
XBRL U.S., a non-profit organization (a spinoff from AICPA), under contract with the 
SEC, created the first U.S. GAAP taxonomy in 2008. Like a dictionary, the taxonomy includes a 
 
6 In 2005, the SEC established a voluntary XBRL filing program to prepare companies for the submission of XBRL 
filings. Through April 2008, over 75 companies voluntarily filed in XBRL. See Bartley et al. [2011], Efendi et al. 
[2016], and Hsieh and Bedard [2018] for studies on the voluntary filing program. 
7 The tagging requirement is exempt for a few types of quantitative values in footnotes, such as those in “the $1.99 
pancake special,” “1% fat milk,” and “drilling 700 feet” (see https://www.sec.gov/corpfin/interactive-data-cdi; 
Question 146.16). 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 15

13 
 
standard list of tags for financial statement items and associated contextual information for 
software to recognize and process without human intervention. The contextual information 
includes definitions, authoritative references to U.S. GAAP/SEC regulations, and calculation 
relationships with other tags (e.g., Accounts Receivable, Net = Accounts Receivable, Gross – 
Allowance for Doubtful Accounts). The FASB took over the maintenance of the taxonomy from 
XBRL U.S. after the SEC mandate of 2009 and has updated it every year since 2011. The annual 
update occurs for reasons such as changes in accounting standards, technical corrections, and the 
actual use of tags. 
Preparers must tag the quantitative items in the financial reports with the appropriate 
elements from the standard list. Appendix A provides two examples. In the first example, the 
amount of cash and cash equivalents on the balance sheet is tagged by 
“CashAndCashEquivalentsAtCarryingValue” in the XBRL document. The opening tag also 
contains contextual information about the taxonomy (“us-gaap”), the unit (“usd”), the period 
(“AsOf29Dec2012”), and the decimal points for presentation (“-3” for in thousands). In the 
second example, the amount of work in process inventory, as disclosed in a footnote, is tagged 
by “InventoryWorkInProcess.” When there is no appropriate tag in the standard list for a 
financial concept, a company can create a company-specific tag, called an “extension.” The 
mandate does not require companies to obtain assurance on the XBRL filings or involve third 
parties, such as auditors or consultants.8 The XBRL documents submitted within 24 months 
since the initial adoption are protected from liability for failure to comply with the tagging 
requirements (SEC [2009]). 
 
8 Plumlee and Plumlee [2008] and Boritz and No [2009] discuss the potential challenges of XBRL documents’ 
assurance. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 16

14 
 
 
While the mandate is intended to improve financial reports’ usefulness, research 
documents data quality issues with the XBRL-tagged data in early adoption years. Debreceny et 
al. [2010] find that one quarter of the XBRL filings by the initial 400 large companies in the first 
round of submissions had errors such as misuse of debit/credit, missing values in calculation 
relationships, and wrong values. Debreceny et al. [2011] take a close look at extensions in XBRL 
filings of 67 large accelerated filers in the first round of submission. They find that 41 percent of 
them are unnecessary as appropriate tags already exist in the taxonomy, likely due to premature 
search in the taxonomy or inadequate understanding of the tagging structure. Some tags include 
information by category using dimensions (e.g., for segment reporting or further disaggregation 
of property assets). Although the U.S. GAAP taxonomy provides some standard dimensions, 
SEC [2016] reports that 50 percent of filers use custom dimensions, significantly compromising 
dimensional data comparability. The errors, custom tags, and tags with dimensions compromise 
the effective use of the XBRL-tagged financial data (Harris and Morsfield [2012]). 
Despite the complaints, the SEC, XBRL U.S., and third parties continue to invest in 
improving the data quality. The SEC periodically issues staff observations, updates to filer 
practices, and even “Dear CFO” letters on XBRL quality.9 Michael Willis, the assistant director 
of the SEC Office of Structured Disclosure, states that the commission is focusing on data-driven 
regulation, developing data quality tools, and working with the FASB on U.S. GAAP taxonomy 
enhancements.10 The Data Quality Committee of XBRL U.S. sets guidance and validation rules 
to prevent or detect inconsistencies or errors in XBRL documents. The committee also collects 
 
9 For example, in July 2014, the SEC Division of Corporation Finance sent letters to certain companies regarding the 
requirement to include calculation relationships in the XBRL filings 
(https://www.sec.gov/divisions/corpfin/guidance/xbrl-calculation-0714.htm). 
10 See “SEC’s Increasingly Sophisticated Use of XBRL-Tagged Data” at 
https://www.undergrad.haslam.utk.edu/sites/default/files/files/SECs_Increasingly_Sophisticated_Use_of_XBRL_Ta
gged_Data.pdf. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 17

15 
 
and publishes real-time errors in XBRL filings.11 Third-party filing service companies such as 
XBRL Cloud also monitor for data quality issues in XBRL filings.12 Using a sample of over 
4,000 XBRL filings from 2009 to 2010, Du et al. [2013] find a reduction in the number of errors 
per filing. Blankespoor [2019] computes the number of unique user-day-filing downloads of 
XBRL filings from the SEC’s EDGAR website by year. She finds that the number rises from 
about 1 million in 2012 to 6 million in 2014, suggesting an increasing demand for XBRL-tagged 
financial data. 
 
3. Data and Approach to Prediction 
3.1. DATA 
Table 1 shows our sample selection process. We first obtain XBRL 10-K and 10-K/A 
submissions between June 15, 2012 and March 31, 2018 from the SEC’s website.13 To take 
advantage of detailed footnote disclosures in XBRL, we restrict our sample to submissions with 
a reporting period ending on or after June 15, 2012. Recent studies demonstrate that earnings 
used by analysts are of higher quality and more value-relevant to investors, relative to GAAP 
earnings and non-GAAP earnings reported by managers (Bentley et al. [2018]; Bradshaw et al. 
[2018]). As such, we use I/B/E/S-reported EPS to compute annual earnings changes. After 
merging XBRL documents with pro forma earnings from I/B/E/S, we obtain 10,073 
 
11 The errors can be found at https://xbrl.us/data-quality/filing-results/. 
12 See https://edgardashboard.xbrlcloud.com/edgar-dashboard/. 
13 Starting from 2014, the SEC parses all the XBRL documents and puts the XBRL-tagged items in relational 
databases, available for bulk download at https://www.sec.gov/dera/data/financial-statement-data-sets.html. We 
examine annual reports for two reasons. First, many disclosures are not required for quarterly reports, making the 
fourth-quarter data incomparable to those in the previous three. For example, the Statement of Stockholders’ Equity 
was not required in 10-Q filings prior to 2018. Second, this design facilitates the comparison between our study and 
Ou and Penman [1989]. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 18

16 
 
submissions.14 We require that these companies have share price data from CRSP, yielding 8,381 
submissions. Requiring non-zero total assets from the XBRL documents leads to a sample of 
8,358 submissions. We leverage the point-in-time nature of XBRL submissions by retaining only 
the most recent financial data as of three months after the fiscal year end, resulting in a sample of 
8,149 submissions.15 Panels A and B of Table 2 report the number of XBRL submissions by 
calendar period and by industry, respectively.16 As expected, there are only 119 submissions of 
XBRL documents for 10-K filings in 2012 as the detailed footnote tagging for all firms is 
available only after June 15, 2012. 
A submission contains both numerical and contextual data. Retaining only the numerical 
data, we obtain 167,136 distinct tag names (for both custom and standard tags) from the 8,149 
submissions. Figure 4 Panel A shows a histogram by the number of distinct tag names. More 
than 30 percent of submissions use 250 to 300 distinct tags, and an average submission uses 284 
distinct tags. For each submission, we divide the number of distinct custom tags by the number 
of distinct tags (i.e., the proportion of custom tags) and plot a histogram by this variable in Panel 
B. For about 30 percent of submissions, 15 to 20 percent of distinct tags are extensions, and the 
average proportion of extensions is 15.5 percent. Some standard tags are deprecated, and some 
are added over the years due to changes in accounting standards. We identify uncommon 
standard tags as those that have not been used at least once in each year of our 2012–2018 
 
14 When we use U.S. GAAP EPS to calculate earnings changes and do not require pro forma earnings, the final 
sample size increases. Our inferences are unchanged by using this sample but become weaker (see Online Appendix 
Table A1), consistent with U.S. GAAP earnings being less informative about fundamentals than pro forma earnings 
(Bentley et al. [2018]; Bradshaw et al. [2018]). 
15 We keep only XBRL documents filed before the end date of three months after the fiscal year end. If a company 
has an XBRL 10-K submission and an XBRL 10-K/A submission to revise financial statements (but not footnotes) 
before the end date, we merge the two submissions by using the revised financial statement items from the 10-K/A 
and the footnote items from the 10-K. 
16 We include the financial industry (banking, insurance, real estate, trading) to fully explore investment space. 
Nevertheless, excluding firms in this industry does not alter our inferences (see Online Appendix Table A2). 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 19

17 
 
sample period.17 Panel C presents a histogram by the proportion of uncommon standard tags 
(i.e., the number of distinct uncommon standard tags divided by the number of distinct tags). 
Close to 40 percent of submissions contain 2 to 4 percent uncommon standard tags, and the 
average proportion of these tags is 4.5 percent, suggesting no major changes to standard tags. As 
our prediction analysis requires predictors to be populated across firms, we exclude all custom 
and uncommon standard tags, yielding 4,627 distinct common standard tags.18 Panel D shows a 
histogram by the number of these tags. We also discard disaggregate items tagged with 
dimensions, since they are mostly firm-specific (see Section 2.2) and thus cannot be used in a 
large sample.19 
In some cases, identical tags are used to describe financial data for a co-registrant, for 
example a guarantor subsidiary. We retain only the consolidated data. Companies use identical 
tags in a document to refer to items of different reporting periods. For instance, multiple items 
are identically tagged as “NetIncomeLoss” spanning different reporting periods such as current 
and prior years. For each of the 4,627 tags, we select current and prior fiscal year data and 
compute the percentage changes, which creates 13,881 predictors. Then, for predictors with 
missing values, we fill in zeros.20 
 
17 For example, a standard tag “UnrecognizedTaxBenefitsResultingInNetOperatingLossCarryforward” was 
deprecated in the 2014 U.S. GAAP taxonomy with the implementation of ASU 2013-11 about income taxes in 2014. 
18 Given the proportion of custom tags, the drastic drop in the number of distinct tag names (from 167,136 to 4,627) 
is due to the firm-specific nature of custom tags. For example, suppose there are 1,000 documents; each document 
contains 200 standard tags and 50 custom tags that other firms never use. The average proportion of custom tags 
across the 1,000 documents is 20% (=50/250), but custom tags account for 50,000 (= 50 × 1,000) out of 50,200 (= 
200 + 50 × 1,000) distinct tags. To preserve more standard tags, we also define uncommon standard tags as those 
that have not been used at least once in each year of the four-year rolling window (i.e., two years of a training 
sample, one year of a validation sample, and one year of a test sample). Using this alternative definition does not 
affect our inferences (see Online Appendix Table A3). 
19 The exclusion of items tagged with dimensions causes the loss of some disaggregated detailed information about 
the primary financial statement items and thus can substantially diminish the potential predictive power of XBRL-
tagged data. How to incorporate information tagged with dimensions is an interesting question for future research. 
20 Creating an indicator variable for missing values in each predictor, which will double the number of predictors, 
does not alter our inferences (see Online Appendix Table A4). We also use the industry-year average to impute 
missing values and obtain similar results (see Online Appendix Table A5). Dropping the percentage change 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 20

18 
 
The FASB maps the tags in each U.S. GAAP taxonomy to financial statement categories. 
The map is “organized to roughly correspond to the arrangement of elements in the order in 
which they might be found in a financial statement” (FASB [2018]).  Using this map, we classify 
the predictors into six categories: balance sheet, income statement, cash flow statement, 
comprehensive income statement, shareholders’ equity statement, and footnote disclosures. A tag 
may be associated with both a financial statement and footnote disclosures (e.g., 
“InventoryNet”), as a company refers to a financial statement item in a footnote when disclosing 
more information about that item. We classify the tag into the corresponding financial statement. 
This procedure allows us to classify 4,503 of 4,627 tags. The remaining 124 tags are mapped to 
multiple financial statements. We manually assign them to the statement with a natural fit (see 
Online Appendix Table A8). Panel A of Table 3 shows that a substantial portion of the predictors 
belongs to footnotes. Panels B to G list the top 10 most populated (i.e., non-zero) current 
predictors by financial statement category and present descriptive statistics for the predictor 
values across the 8,149 submissions. Finally, we scale the current and lagged predictors by total 
assets (except for total assets itself and items on a per-share basis). 
3.2. APPROACH TO PREDICTION 
3.2.1. Direction of Earnings Changes. We examine the direction of earnings changes for 
several reasons. First, it is difficult to predict the level of future earnings, as extant studies find 
that earnings forecasts based on firm characteristics are not substantially more accurate than 
forecasts obtained from the random-walk model (Kothari [2001]; Gerakos and Gramacy [2013]; 
Li and Mohanram [2014]). This also means that it is challenging to forecast the amount of future 
 
predictors does not affect our inferences (see Online Appendix Table A6). Also, adding the Fama and French 30 
industry indicators to the models does not affect our inferences (see Online Appendix Table A7). This is 
unsurprising as many items unique to certain industries (e.g., 
“CapitalizedSoftwareDevelopmentCostsForSoftwareSoldToCustomers”) already capture the industry effects. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 21

19 
 
earnings changes, which is equivalent to forecasting the level of future earnings minus known 
current earnings. Second, Freeman et al. [1982, 643] argue that the substantial variability in 
earnings changes relative to the amount of information in explanatory variables makes it difficult 
to form accurate forecasts of earnings changes. They propose to reduce the variability in earnings 
changes by considering the easier-to-predict direction rather than the amount of the changes. 
While losing some variation, this binary specification helps mitigate the concern about low out-
of-sample performance from predicting the amount of earnings changes.21 Third, forecasting the 
sign of earnings changes is economically meaningful and actionable, as previous studies 
construct portfolios based on the direction of earnings changes (Ou and Penman [1989]; Ou 
[1990]; Wahlen and Wieland [2011]). 
Following the literature of predicting the direction of earnings changes (Freeman et al. 
[1982]; Ou and Penman [1989]), we adjust for the firm-specific trend by subtracting the average 
change in EPS over the past four years from the current EPS changes. An earnings 
increase/decrease is coded after taking out the drift term. This procedure helps us accomplish 
three goals. First, since earnings increases tend to outnumber earnings decreases, taking out the 
drift term mitigates the class imbalance issue (Freeman et al. [1982, 645]; Japkowicz and 
Stephen [2002]). Second, as some earnings changes are anticipated due to drift, predicting the 
direction of de-trended earnings changes is more useful for investment decisions.22 Third, the 
 
21 Ou and Penman [1989, 298] argue: “There is a loss of information in the binary specification, but we were 
concerned that, given outliers common to accounting data, estimation with dollar magnitudes might produce 
parameter estimates that perform poorly in out-of-sample prediction and result in investment strategies that give 
undue weight to estimation errors.” While focusing on the direction of earnings changes, we examine the 
predictability of the machine learning methods concerning the level of earnings and the amount of earnings changes 
in Section 4.5. 
22 Our final sample from 2012 to 2018 consists of 3,610 earnings increases and 4,539 earnings decreases. Without 
taking out the drift term, the numbers are 5,418 and 2,731, respectively. Note that this drift adjustment uses only 
past information and thus does not rely on any foresight. We also use an alternative way to remove the firm-specific 
trend by subtracting the lagged change in EPS, which overlaps the percentage change predictors in time, from the 
current change in EPS. No inference is affected (as shown in Online Appendix Table A9). Another way to account 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 22

20 
 
drift adjustment permits a direct comparison of our models with the literature (i.e., Ou and 
Penman [1989]). Nevertheless, our results are robust to using the sign of earnings changes 
without de-trending (see Online Appendix Table A11). 
3.2.2. Parameters. When choosing parameters to tune in a machine learning model, we 
need to balance prediction accuracy and computational cost. While trying more values 
potentially improves prediction accuracy, the price paid for these improvements is increasingly 
more computational time. In many cases, using a large set of values for parameter tuning is 
computationally prohibitive. As such, we set these parameters following standard practice in 
machine learning when available or around default values offered by R packages.23 Table 4 
shows the parameters of the two machine learning methods. In random forests, the dropout 
convention is to randomly select 𝑘𝑘= ඥ𝑝𝑝 variables for consideration in each tree, where 𝑝𝑝 is the 
number of predictors (Breiman [2001]). As such, we choose the integers between 110 and 120 
for this dropout procedure. We allow the machine to grow 500 to 2,000 trees with an increment 
of 100 and bootstrap 50 percent of the sample for each tree.24 The minimum number of 
observations in a leaf (i.e., terminal node) are integers from 1 to 4. For stochastic gradient 
boosting, the machine can grow 500 to 2,000 trees with an increment of 100 with three possible 
 
for anticipation due to drift is comparing actual earnings in fiscal year t + 1 with the consensus analyst forecast 
issued in the month following the earnings release for fiscal year t. In other words, one can use the sign of analysts’ 
forecast errors to proxy for earnings changes’ direction. However, if analysts incorporate financial information more 
than the drift into their forecasts, the predictive power of our explanatory variables will deteriorate. To make the 
predictability of detailed financial data independent of analysts’ ability and to make our models comparable to prior 
research (e.g., Ou and Penman [1989]), we do not adopt this alternative strategy in our primary analyses, but report 
the results of using this alternative in Online Appendix Table A10. 
23 We use the package “randomForest” for random forests and “gbm” for stochastic gradient boosting (also see 
footnote 39 for loss functions used to compute variable importance). The default values in “randomForest” are 500 
trees, a minimum of 1 observation in a leaf, and 1 for bagging. The default values in “gbm” are 100 trees, a learning 
rate of 0.1, a tree depth of 1, a minimum of 10 observations in a leaf, and 0.5 for bagging. 
24 We set the bagging parameter to 0.5 for random forests to enable comparison with stochastic gradient boosting. 
Nevertheless, using the bagging parameter of 1 yields a similar result (an AUC of 67.53). 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 23

21 
 
learning rates (0.005, 0.01, and 0.05).25 We randomly pick 50 percent of the sample to estimate 
each tree. The early stopping criteria for gradient boosting are typically stricter (than random 
forests), as the idea is to chain a series of weak learners to mitigate overfitting. As such, we set 
the tree depth to 1 to 4 and set the minimum number of observations in a leaf to 10. The best 
parameters are chosen using the sample splitting method. 
3.2.3. Sample splitting. In machine learning, the data are typically split into training, 
validation, and testing samples. Models are estimated using the training sample, selected via the 
validation/hold-out sample (i.e., tuning the parameters), and then applied to the test sample. A 
common approach to tuning the parameter is n-fold cross-validation. This method splits the 
sample randomly into n folds and fits the model by excluding one fold as a hold-out sample, 
which is then used for model evaluation; this procedure is repeated by rotating the excluded fold 
and selects parameters that maximize the average performance on hold-out samples. Since our 
dataset is a panel and predicting future earnings is intertemporal in nature, conducting the n-fold 
cross-validation is inappropriate: the random partition and rotation of the hold-out samples 
would imply using past events (e.g., an earnings increase in 2012) to evaluate a model estimated 
from some future data (e.g., earnings increases/decreases in 2014), inconsistent with our 
chronological earnings prediction task. As such, we split the training, validation, and test 
samples temporally. Specifically, we use a rolling sample splitting scheme, in which the training 
and validation samples gradually shift forward in time, but the number of years in each sample is 
held constant. For each year in the test period from 2015 to 2018 (e.g., 2015), the models are 
trained in the second and third preceding years (e.g., 2012–2013) and validated in the preceding 
 
25 We set the minimum number of trees (500) higher than the default value (100) in “gbm” to make it comparable 
with random forests. Nevertheless, if we allow the machine to grow from 100 to 2,000 trees with an increment of 
100, the chosen model always consists of more than 500 trees and exhibits similar predictive power (an AUC of 
67.53). 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 24

22 
 
year (e.g., 2014) to tune the parameters as shown in Table 4. The training sample always starts 
fresh in the second and third preceding years and thus has the benefit of using more recent 
information for model construction. 
3.2.4. Model Performance. We use two metrics to evaluate out-of-sample performance. 
The first is AUC, which is equivalent to the probability that a randomly chosen earnings increase 
will be ranked higher by a classifier than will a randomly chosen earnings decrease observation 
(Fawcett [2006]). While commonly used in classification problems, this measure offers little 
economic meaning for prediction gains. To better quantify these gains, we also use excess 
returns to easily implementable hedge portfolios as the second measure. Once we apply the 
estimated model to the year in the test period, we obtain the summary measure 𝑃𝑃𝑃𝑃
෢, the predicted 
probability of an earnings increase in the next year. A hedge portfolio is then formed based on 
this measure. Specifically, each stock in the sample is assigned to a long (short) position three 
months after its fiscal year-end, when 𝑃𝑃𝑃𝑃
෢ > 0.5 or 0.6 (< 0.5 or 0.4). The positions are held for 12 
months, from three months after the fiscal year-end, when all the predictor values are publicly 
available as the 10-K has been filed, to three months after the next fiscal year-end, when the next 
annual earnings have been released.26 We measure excess returns using the size-adjusted returns 
(SAR), which are commonly used in accounting and finance studies (Piotroski and So [2012]; Li 
and Mohanram [2019]). For stock i, it is calculated as  
 
26 On average, the next earnings announcement is 320 days after the portfolio formation date and 45 days before the 
portfolio end date, and the next 10-K filing is 331 days after the portfolio formation date and 34 days before the 
portfolio end date. For several reasons, we do not form the portfolio immediately after the 10-K filing and liquidate 
it several days after the next earnings announcement but before the next 10-K filing. First, the trading horizon varies 
across firms, making the excess returns less comparable. Second, this strategy requires more effort in predicting the 
next 10-K filing dates. Third, for 18 (9) percent of firms, the earnings announcement and 10-K filing occurred on the 
same day (in two consecutive days), making it challenging to implement such a strategy. Nevertheless, we 
acknowledge that the returns of our strategy contain noise to the extent that stock prices incorporate some 
information in the next 10-K filing by the portfolio end date. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 25

23 
 
𝑆𝑆𝑆𝑆𝑅𝑅𝑖𝑖= ෑ(1 + 𝑅𝑅𝑖𝑖𝑖𝑖)
12
𝑡𝑡=1
−ෑ(1 + 𝑅𝑅𝑠𝑠𝑠𝑠)
12
𝑡𝑡=1
, 
where 𝑅𝑅𝑖𝑖𝑖𝑖 is the return on stock i in month t, and 𝑅𝑅𝑠𝑠𝑠𝑠 is the value-weighted returns on the market 
capitalization-matched decile portfolio in month t. When computing SAR, we use the NYSE 
breakpoints to assign each stock to its corresponding size decile (Hou et al., [2020]). Moreover, 
the return data are corrected for delisting bias, as suggested by Shumway [1997] and Shumway 
and Warther [1999]. The results are stronger when we use market-adjusted returns, for which 𝑅𝑅𝑠𝑠𝑠𝑠 
is replaced with the value-weighted returns on the market portfolio in month t, 𝑅𝑅𝑚𝑚𝑚𝑚 (see Online 
Appendix Table A12). 
 
4. Results 
4.1. OUT-OF-SAMPLE PERFORMANCE OF MACHINE LEARNING MODELS 
 
Table 5 reports the out-of-sample prediction performance in the 2015–2018 test period 
for all the observations (𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5) and observations excluding borderline cases 
(𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4).27 The distribution of observations by year (in Table 1 Panel A) and the 
rolling windows for machine learning (in Section 3.2.3) can be used to compute the training and 
validation sample size for each year in the test period. There are 5,520 observations in the entire 
test period. Among predicted increases, 60.05 to 65.64 percent are actually earnings increases. 
We also observe that 61.9 to 67.5 percent of observations are correctly predicted. Specifically, 
28.12 to 33.07 (86.69 to 94.64) percent of earnings increases (decreases) are correctly predicted, 
suggesting that the symmetric cutoffs are too stringent (lenient) for earnings increases 
 
27 The chosen parameter values for each method are reported in Online Appendix Table A13. The values are 
relatively stable over time and do not cluster on the lower or upper bounds, suggesting that the allowed range for 
each parameter is typically not binding. For example, in only one of eight cases (two methods × four test years), the 
chosen number of trees is at the boundary (500; stochastic gradient boosting for the test year of 2017). 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 26

24 
 
(decreases). For example, using 0.4 as the cutoff (𝑃𝑃𝑃𝑃
෢> 0.4 and 𝑃𝑃𝑃𝑃
෢≤ 0.4), we observe that 61.64 
(63.01) percent of earnings increases (decreases) are correctly predicted by random forests, and 
57.13 (66.98) percent of earnings increases (decreases) are correctly predicted by stochastic 
gradient boosting. We next turn to the AUC, which does not rely on specific cutoffs. 
The AUC ranges from 67.52 to 68.66 percent depending on methods (random forests or 
stochastic gradient boosting) and samples (the full sample or the sample with 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 
0.4), significantly higher than 50 percent of a random guess.28 Following Carpenter and Bithell 
[2000], we construct a bootstrap p-value for the difference between our AUCs and 50 percent. 
Specifically, we use a bootstrap sample with the same size as the original test sample to compute 
a bootstrap AUC and repeat this 10,000 times. The p-value is the proportion of 10,000 bootstrap 
AUCs that are below 50 percent. All the p-values are less than 0.01, indicating that our models’ 
predictive power is unlikely to be a random outcome.29 The results suggest that the machine 
learning models extract meaningful signals from the detailed financial data. 
 
Table 5 also reports the size-adjusted returns over the 12 months on the portfolios 
constructed according to the estimated summary measure 𝑃𝑃𝑃𝑃
෢ using machine learning and detailed 
financial data. A hedge portfolio with a long (short) position for stocks with 𝑃𝑃𝑃𝑃
෢> 0.5 (𝑃𝑃𝑃𝑃
෢≤ 0.5) 
yields size-adjusted returns of 5.02 percent for random forests and 6.57 percent for stochastic 
 
28 A random walk model (with drift) predicts an equal probability of an earnings increase and decrease (net of drift), 
which represents only one point (0.5, 0.5) on the ROC curve of a random guess (the diagonal line; Fawcett [2006, 
863]). We also consider two simple classifiers, although they have not been developed in the literature: (1) 
predicting an earnings increase (net of drift) next year if there is an earnings increase (net of drift) in the current year 
and (2) predicting an earnings increase (net of drift) next year if there is an earnings decrease (net of drift) in the 
current year. The two classifiers exhibit an AUC of 46.54 and 53.46 percent, respectively, lower than our models’. 
Unsurprisingly, the sum of the two AUCs equals one as (2) is a flip of (1). It is also consistent with the intuition that 
the drift adjustment makes the first classifier not work well. If we do not take out drift from earnings changes, the 
two classifiers exhibit an AUC of 56.33 and 43.67 percent, respectively. 
29 To address the issue related to overlapping training/validation sets for test years (2015–2018), we construct a 
bootstrap p-value for each test year and observe that all the p-values are less than 0.01. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 27

25 
 
gradient boosting. These returns account for 38.7 and 50.7 percent of returns from a strategy with 
perfect foresight of the direction of one-year-ahead earnings changes (i.e., 12.97 percent). 
To assess the extent to which the returns are generated by chance, we place them in the 
distribution of hedge returns under the null hypothesis that 𝑃𝑃𝑃𝑃
෢ is unrelated to subsequent stock 
returns. Specifically, for each model, we randomly draw with replacement the same number of 
stocks as those in the long and short positions, compute the 12-month size-adjusted returns for 
this pseudo hedge portfolio, and repeat this process 10,000 times. The p-values less than 0.01 for 
both returns (5.02 and 6.57 percent for random forests and stochastic gradient boosting, 
respectively) suggest that they are unlikely to be random outcomes. When we exclude the 
borderline cases and take a long (short) position for stocks with 𝑃𝑃𝑃𝑃
෢> 0.6 (𝑃𝑃𝑃𝑃
෢≤ 0.4), the size-
adjusted returns are more impressive: 9.43 percent for random forests and 9.74 percent for 
stochastic gradient boosting.30 
4.2. THREE BENCHMARKS 
 
To learn more about the out-of-sample performance of our models, we compare them 
with three benchmarks. The first two are conventional regression models, with one using a 
“kitchen sink” approach and the other using the DuPont Analysis to select predictors. The last 
benchmark comes from forecasts of professional analysts. 
4.2.1. Ou and Penman [1989]. Following Ou and Penman [1989], we estimate logistic 
regressions using 65 accounting variables, which typically appear in financial statement analysis 
 
30 In additional analyses reported in the Online Appendix, we observe the following patterns of the excess returns. 
(1) The returns increase monotonically as 𝑃𝑃𝑃𝑃
෢ moves from below 0.1 to above 0.8 (Table A14) and are concentrated 
in the long positions (Figure A1). (2) The excess returns persist after accounting for transaction costs estimated as 
the effective bid-ask spread following Novy-Marx and Velikov [2016] (Table A15). (3) The excess returns are 
robust to using two alternative metrics (ROE and EBIT per share) to measure the direction of earnings changes 
(Table A16 Panel A), excluding microcaps (Table A16 Panel B), accounting for Fama and French’s [2015] five risk 
factors (market returns in excess of the one-month T-bill rate and returns on Size, book-to-market, profitability, and 
investment portfolios) (Table A16 Panel C). (4) No inference is affected if we use size-and-book-to-market-adjusted 
returns (Green et al. [2011]) (Table A17 and Figure A2). 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 28

26 
 
textbooks.31 Their model represents a “kitchen sink” approach with a large number of predictors. 
For each year in the test period (2015–2018), we use the past three years to estimate a logistic 
model and then apply the model to the test year. Their variable selection approach consists of 
three steps. First, they run a univariate logistic regression for each of the 65 variables and retain 
only variables that load significantly at the 10 percent level. Second, a logistic regression is 
estimated using all remaining variables simultaneously. All variables with coefficients that are 
not significant at the 10 percent level are dropped. In the final stage, for the remaining variables, 
they delete the variables that do not load significantly at the 10 percent level stepwise until all 
explanatory variables have statistically significant coefficients at the 10 percent level. This 
stepwise elimination helps remove redundant variables and improve model fitting. We strictly 
follow each step of their approach and refer to the final logistic model as OP/Logit. To better 
understand the differences between our random forests and stochastic gradient boosting models 
(XBRL/RF and XBRL/SGB, respectively) and OP/Logit, we also apply the machine learning 
methods to the 65 variables and refer to the two models as OP/RF and OP/SGB. 
As shown in Table 5 Panel B, our models significantly outperform OP/Logit by a large 
margin. The XBRL/RF (XBRL/SGB) model exhibits an AUC of 67.52 (67.54) percent, 
compared with 61.79 percent for OP/Logit. We also observe an AUC of 66.63 (66.87) percent 
for the OP/RF (OP/SGB) model, which is significantly higher than that of OP/Logit and 
marginally lower than XBRL/RF (XBRL/SGB). Following Carpenter and Bithell [2000], we 
construct a bootstrap p-value for the AUC difference. Specifically, for each comparison between 
two data/method combinations, we use a bootstrap sample with the same size as the original test 
 
31 Ou and Penman [1989] use 68 financial variables. We exclude three variables (%∆ in total uses of funds, %∆ in 
total sources of funds, and %∆ in funds) as they are no longer reported. None of the three variables is statistically 
significantly associated with the direction of one-year-ahead earnings changes in Ou and Penman [1989]. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 29

27 
 
sample to compute a bootstrap AUC for each combination and repeat this 10,000 times. The p-
value is the proportion of 10,000 bootstrap AUC differences that are below zero. We observe 
that all the p-values are less than 0.1 except for XBRL/SGB vs. OP/SGB, for which the p-value 
is 0.108. Thus, the improvements in predictive power are unlikely to be random outcomes.32 
We observe a similar pattern in hedge portfolio returns. The strategy of applying machine 
learning methods to the 65 variables generates size-adjusted returns of 4.67 percent for OP/RF 
and 3.91 percent for OP/SGB. These returns are significantly higher than 2.48 percent from Ou 
and Penman’s original strategy (OP/Logit), and marginally lower than 5.02 percent for 
XBRL/RF and 6.57 percent for XBRL/SGB. We conduct a bootstrap test for the difference in 
returns between each pair of portfolios (i.e., OP/Logit vs. OP/RF, OP/RF vs. XBRL/RF, 
OP/Logit vs. OP/SGB, and OP/SGB vs. XBRL/SGB). Specifically, we randomly draw with 
replacement the same number of stocks as those in the long and short positions for each portfolio 
in a pair and compute the 12-month size-adjusted return for the pseudo hedge portfolio. We then 
take a difference in returns between the two pseudo hedge portfolios and repeat this process 
10,000 times. The p-value is based on the actual difference with respect to the distribution of 
simulated differences. The p-values are less than 0.01 for all pairs. Overall, the results suggest 
that our models’ superior performance comes from primarily flexible functional forms in 
machine learning and secondarily more detailed financial information in XBRL documents. 
4.2.2. DuPont Analysis. Unlike the “kitchen sink” approach in Ou and Penman [1989], 
Nissim and Penman [2001] derive eight drivers of earnings based on the DuPont Analysis. The 
 
32 The results for OP/RF vs. XBRL/RF and OP/SGB vs. XBRL/SGB should be interpreted with caution, as none of 
them is statistically significant if the significance threshold of 5% is used. To address the issue related to 
overlapping training/validation sets for test years (2015–2018), we construct the pairwise bootstrap p-values for each 
test year and observe that all the p-values are less than 0.1 except for XBRL/RF vs. OP/RF and XBRL/SGB vs. 
OP/SGB in 2015, which is unsurprising, given the data quality issues in the early years in the training sample (2012–
2013) for 2015. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 30

28 
 
eight drivers are sales profit margin, asset turnover, other items divided by net operating assets, 
financial leverage, net borrowing cost, return on net operating assets, operating liability leverage, 
and minority interest sharing (Nissim and Penman [2001, 118]). These expert-identified 
variables could improve predictive performance. 
For each year in the 2015–2018 test period, we use the past three years to estimate a 
logistic model using levels of and changes in the eight variables to predict the direction of one-
year-ahead earnings changes (net of drift).33 The estimated coefficients are then applied to the 
test year. The resulting forecasts from this analysis exhibit an AUC of 57.96 percent and annual 
size-adjusted returns of 1.90 percent, significantly lower than those of our models. Nissim and 
Penman [2001] acknowledge that while these eight drivers map into current earnings, their 
predictive power depends on their time-series behavior, which is not explicitly developed. 
As the logistic model does not accommodate the multiplicative nature of the DuPont 
Analysis (e.g., profit margin and asset turnover), we apply the machine learning methods to the 
levels of and changes in the eight drivers (DuPont/RF and DuPont/SGB). DuPont/RF 
(DuPont/SGB) exhibits an AUC of 61.51 (61.15) percent and size-adjusted returns of 2.73 (2.12) 
percent, which are significantly higher than those of DuPont/Logit with all bootstrap p-values < 
0.01 and lower than those of XBRL/RF (XBRL/SGB) with all bootstrap p-values < 0.01. The 
results highlight the usefulness of both machine learning and detailed financial information in 
earnings prediction. 
4.2.3. Analysts’ Forecasts. Professional analysts have access to machine learning, 
detailed financial information from 10-K filings, and other sources of information. Their 
forecasts could dominate our models and thus serve as the third benchmark. We take analysts’ 
 
33 Soliman [2008] estimates a similar model with the linear combination of levels of and changes in DuPont 
components to explain future returns on net operating assets. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 31

29 
 
forecasts in the month following the portfolio formation when all the detailed financial 
information in 10-Ks is available and compare each forecast with the realized earnings net of 
drift in fiscal year t (i.e., subtracting drift from the difference between a forecast and realized 
earnings) to determine whether an analyst forecasts an earnings increase or decrease for the 
covered firm. The analysts’ prediction is the proportion of forecasts that indicate an earnings 
increase. Table 5 Panel B shows an AUC of 65.09 percent for analysts’ prediction of an earnings 
increase, significantly lower than our models (bootstrap p-value < 0.01). A hedge portfolio with 
a long (short) position for stocks with the analysts’ prediction of an earnings increase (decrease) 
> 0.5 (≤ 0.5) yields size-adjusted returns of 3.38 percent, lower than those from our models.34 
The results highlight the usefulness of machine learning and detailed financial information in 
earnings prediction, even in the presence of professional forecasters, who may have limited 
ability to process detailed financial data. Figure 5 Panel A reports the ROC curves for our 
machine learning models and the benchmarks. Consistent with the results in Table 5, the figure 
shows that our models dominate all of them. 
4.3. USING COMPUSTAT AS AN ALTERNATIVE SOURCE OF DETAILED FINANCIAL 
INFORMATION 
We use Compustat as an alternative source of detailed financial information to XBRL. 
Compared with XBRL-tagged data, Compustat has its own advantages, such as more extensive 
 
34 If we do not subtract drift from the difference between analysts’ forecasts and realized earnings to determine the 
analysts’ prediction, the AUC and size-adjusted returns are 64.71 and 3.93 percent, respectively. If we compare the 
consensus (i.e., median) analyst forecast with the realized earnings in fiscal year t to define the analysts’ prediction, 
the AUC and size-adjusted returns are 63.62 and 2.49 percent, respectively. The decreased predictive power is 
unsurprising since the median forecast misses information from the entire forecast distribution (e.g., Q1 and Q3). 
We also examine whether consensus analyst forecasts help improve our hedge returns to the extent that some 
earnings increases/decreases are anticipated and thus do not help earn future excess returns. A hedge portfolio with a 
long (short) position for stocks with 𝑃𝑃𝑃𝑃
෢> 0.5 and an earnings decrease predicted by analysts (𝑃𝑃𝑃𝑃
෢≤ 0.5 and an 
earnings increase predicted by analysts) yields size-adjusted returns of 6.40 percent for random forests and 7.01 
percent for stochastic gradient boosting, higher than the original returns (5.02 and 6.57 percent, respectively). 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 32

30 
 
standardized adjustments to improve data quality, and disadvantages, such as less detailed 
coverage of financial information.35 There are 883 financial items from Compustat, for which we 
take current values, lagged values, and percentage changes, resulting in 2,649 predictors. We 
scale the current and lagged predictors by total assets (except for total assets itself and items on a 
per-share basis). Table 6 Panel A reports the predictive power of detailed financial information 
from Compustat similar to XBRL-tagged data. Figure 5 Panel B reports the ROC curves for our 
machine learning models using the Compustat data. This figure and Table 6 Panel B show that 
our models with the Compustat data continue to outperform the three benchmarks. The results 
also suggest that the benefit of additional financial details relative to Compustat is offset by the 
presence of errors, custom tags, and tags with dimensions in XBRL documents. 
4.4. VARIATION IN DATA QUALITY 
 
We conduct two additional tests based on variation in XBRL data quality. First, since 
errors in XBRL documents are subject to only modified liability within two years after the initial 
adoption (SEC [2009]), we classify the test year 2015 as the early period, the training period 
(2012–2013) for which is fully covered by the liability protection, and 2016–2018 as the late 
period. As shown in Table 7 Panel A, our models exhibit higher AUCs in the late period than the 
early period. The bootstrap p-value for the AUC difference between the two periods is 0.049 for 
random forests and 0.307 for stochastic gradient boosting.36 The average annual size-adjusted 
returns are also higher in the late period. The bootstrap p-value for the SAR difference between 
 
35 The adjustments create discrepancies between the accounting numbers in Compustat and 10-K filings (Chychyla 
and Kogan [2015]). 
36 We also repeat this analysis using all Compustat items, which do not experience the same data quality changes as 
XBRL documents. We observe, as expected, an insignificant AUC difference between the early and late periods (see 
Online Appendix Table A18). 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 33

31 
 
the two periods is less than 0.01 for both models. The results suggest that data quality issues in 
early adoption years decrease the usefulness of detailed financial data in XBRL documents. 
 
Second, we use the proportion of distinct custom and uncommon standard tags in an 
XBRL submission as an inverse measure of data quality (or the “completeness” of data used in 
our models) at the firm level. This measure captures the amount of information lost due to the 
use of extensions and uncommon tags that cannot be used for modeling and thus were removed 
before the analysis.37 We split the test sample by the year median and report the AUC of each 
subsample in Table 7 Panel B. Our models exhibit higher AUCs for firms with high data quality 
than other firms. The bootstrap p-value for the AUC difference between the two subsamples is 
less than 0.01 for both methods. The average annual size-adjusted returns are also higher for 
firms with high data quality. The bootstrap p-value for the SAR difference between the two 
subsamples is 0.014 for random forests and less than 0.01 for stochastic gradient boosting. The 
results suggest that low data quality reduces the predictive power of detailed financial data in 
XBRL documents. 
4.5. PREDICTING THE LEVEL OF EARNINGS AND THE AMOUNT OF EARNINGS 
CHANGES 
 
We examine the direction of earnings changes in primary analyses since it is difficult to 
predict the level of future earnings and the amount of earnings changes (future earnings minus 
known current earnings) and for other reasons discussed in Section 3.2.1. Nevertheless, in this 
section, we use the two machine learning methods to predict the level of earnings and the amount 
of earnings changes following the same rolling windows as in Section 3.2.3. Consistent with 
 
37 We do not claim that the use of the extension per se is inappropriate, but rather that the quality of data used in the 
modeling is lower if the extensions cannot easily be incorporated. As such, this measure should not be interpreted as 
reflecting firms’ data quality in general. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 34

32 
 
prior research, we observe a low out-of-sample R2 of 5.3 (6) percent for random forests 
(stochastic gradient boosting) in predicting the level of one-year-ahead earnings, lower than the 
out-of-sample R2 of 7.5 percent for a simple random-walk model.38 We also observe a low out-
of-sample R2 of 8 (5.8) percent for random forests (stochastic gradient boosting) in predicting the 
amount of one-year-ahead earnings changes. A hedge portfolio with a long (short) position for 
stocks with the predicted earnings changes greater than (less than or equal to) zero yields size-
adjusted returns of 0.10 percent (p-value = 0.47) for random forests and 0.11 percent (p-value = 
0.46) for stochastic gradient boosting. The results suggest that focusing on the direction of 
earnings changes facilitates the success of our machine learning models. 
 
5. Understanding the Inner Workings of the Machine Learning Models 
 
In this section, we seek to understand the inner working of the machine learning models. 
While quantifying each predictor’s importance to the predictive power, we caution that the 
reader should not interpret the importance as the causal influence of the predictor. Rather, it is 
used to provide transparency for the items responsible for inferences drawn by the machine 
learning models. 
 
38 Following Gerakos and Gramacy [2013], we apply random forests to their 24 financial variables to predict the 
level of earnings. As they use the root-mean-squared error adjusted by the Consumer Price Index (adjusted RMSE) 
to evaluate model performance, we compute this metric for this model (GG/RF_L) and our models (XBRL/RF_L 
and XBRL/SGB_L), where “L” denotes the level of earnings. We observe an adjusted RMSE of 2.424 for 
GG/RF_L, similar to 2.409 as reported in their Table 2. Our models yield an adjusted RMSE of 1.913 for 
XBRL/RF_L and 1.766 for XBRL/SGB_L, lower than GG/RF_L. One benefit of predicting the direction of earnings 
changes is that the direction is actionable. In contrast, a level of earnings would have to be combined with an 
expectation proxy. Using the consensus (i.e., median) analyst forecast in the month following the portfolio formation 
as such a proxy, we find that a hedge portfolio with a long (short) position for stocks with the predicted earnings 
minus the consensus greater than (less than or equal to) zero yields size-adjusted returns of 0.49 percent (p-value = 
0.29) for random forests and -0.24 percent (p-value = 0.40) for stochastic gradient boosting. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 35

33 
 
 
We estimate each variable’s importance by computing the predictive performance 
decrease when that variable is randomly shuffled.39 Since a model is trained and validated for 
each test year, a predictor has four importance values (one for each test year of 2015–2018) 
under each method (random forests or stochastic gradient boosting). We compute the correlation 
of importance values between two consecutive years across all predictors (i.e., N = 13,881). For 
the three pairs of consecutive years (2015 vs. 2016, 2016 vs. 2017, and 2017 vs. 2018), the 
correlation coefficients are 0.98, 0.98, and 0.98, respectively, for random forests, and 0.82, 0.75, 
and 0.85, respectively, for stochastic gradient boosting. The results suggest that the importance 
of a variable in predicting the direction of one-year-ahead earnings changes is highly stable over 
time. As such, we average the four importance values for each predictor. Table 8 Panel A 
presents the top 10 most important variables. Most of them pertain to earnings, such as 
“NetIncomeLoss” and “EarningsPerShareBasic.” The results suggest that earnings are still the 
most critical metrics among all the financial items in 10-K filings. For stochastic gradient 
boosting, several balance-sheet items and tax-related variables also make into the top 10 list. We 
also observe cash flows from investing activities and SG&A in the top 70 list for stochastic 
gradient boosting and R&D expenses in the top 70 list for random forests. The results suggest 
that investment activities exhibit sizable predictive power for the direction of earnings changes in 
the next year, but the power is not as strong as earnings and tax-related items.40 
 
Our models contain many potentially multicollinear financial items. While this is not an 
issue for building a predictive model, we may underestimate the individual importance of 
 
39 We use the function “varImp(, type = 1, scale = FALSE)” for random forests and function “summary(, method = 
permutation.test.gbm, normalize = FALSE)” for stochastic gradient boosting. The former computes the reduction in 
the average prediction error rate across all trees and the latter calculates the decrease in the binomial deviance loss 
function (see equation 10.22 in Hastie et al. [2009] when K = 2), when each variable is randomly shuffled. 
40 The results are likely due to the focus on one-year-ahead earnings changes; investments and R&D will probably 
be stronger contributors for longer-term earnings predictions. Given the limited number of years of data, we do not 
examine their importance in predicting long-term earnings and leave it to future research. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 36

34 
 
multicollinear predictors. To address this issue, we follow Pedregosa et al.’s [2011] method by 
(1) performing hierarchical clustering on the predictors’ Spearman rank-order correlations, (2) 
imposing a threshold of 0.8 to obtain 8,993 clusters, and (3) randomly picking one predictor from 
each cluster to be kept in the model.41 We observe an AUC of 67.46 percent (67.06 percent) for 
the new random forests (stochastic gradient boosting) model, close to that of the original model. 
More importantly, we estimate the importance of the randomly chosen predictor in the new 
model, assign the importance to all the other predictors in the same cluster, and compute the 
correlation of importance values between the new model and the original one across all 
predictors (i.e., N = 13,881). The two importance values are highly correlated with a coefficient 
of 0.93 (0.80) for random forests (stochastic gradient boosting). The results suggest that 
multicollinearity is unlikely to influence the ranking of predictor importance significantly. 
 
 Figure 6 shows the sum of variable importance by category. In aggregate, footnote 
disclosures contribute the most in forecasting the direction of one-year-ahead earnings changes, 
followed by balance sheet, income statement, and cash flow statement. Comprehensive income 
statement and shareholders’ equity statement contribute the least to the predictive power. The 
results are consistent with footnote disclosures carrying important information for valuation (De 
Franco et al. [2011]).42 As shown in Table 3 Panel A, footnote disclosures contain the most tags 
(2,443 out of 4,627), which can explain their importance in aggregate. Figure 6 also reports the 
mean of variable importance within each category. We observe that items from financial 
 
41 A limitation of this approach is that the reduction in dimensionality (from 13,881 predictors to 8,993 clusters) is 
not substantial as we impose a high threshold of 0.8. 
42 This comparison is within XBRL-tagged data. It is not inconsistent with the previous finding that detailed 
financial data from XBRL documents (including both financial statement and footnote items) are marginally more 
useful than the 65 variables from Compustat under machine learning (e.g., XBRL/RF vs. OP/RF). Compared with 
the 65 variables, footnotes carry additional information, but financial statement items in XBRL documents suffer 
from data quality issues. As such, the usefulness of combined financial statement and footnote items is only 
marginally higher than that of the 65 variables. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 37

35 
 
statements on average play a stronger role than footnote items, but the latter’s importance is still 
considerable. 
Table 8 Panel B shows the top 10 important variables in footnotes (see Online Appendix 
Table A19 for each category’s top 10 most important variables). We observe many tax-related 
items (e.g., “DeferredTaxAssetsValuationAllowance”) in the top 10 list for footnote disclosures, 
consistent with tax items carrying important information on future taxable income (Miller and 
Skinner [1998]; Lev and Nissim [2004]; Hanlon [2005]; Thomas and Zhang [2011]). For 
example, Miller and Skinner [1998] manually collect valuation allowance for deferred tax assets 
for 200 companies and find that it is negatively associated with future taxable income. 
To visualize the association between tax items and 𝑃𝑃𝑃𝑃
෢, we construct partial dependence 
plots (Hastie et al. [2009]).43 Figure 7 Panel A shows a nonlinear negative association between 
the valuation allowance for deferred tax assets (the top predictor from footnotes) and 𝑃𝑃𝑃𝑃
෢ under 
random forests, consistent with Miller and Skinner [1998]. We also observe an interaction effect 
in Panel B: 𝑃𝑃𝑃𝑃
෢ becomes higher when the valuation allowance is lower and lagged operating 
income (the top predictor under random forests) is higher. The results suggest that the valuation 
allowance provides additional details on operating income growth by revealing management’s 
assessment of future taxable income. Panel C presents a nonlinear negative association between 
tax benefits related to the exercise of employee stock options (Hanlon and Shevlin [2002]; the 
top predictor from footnotes) and 𝑃𝑃𝑃𝑃
෢ under stochastic gradient boosting. Panel D shows an 
 
43 In a one-way partial dependence plot, for each value of a predictor (in the x-axis), we force all observations in the 
training sample to assume that value for the predictor without changing any data points for other predictors, compute 
the forecasts using the chosen model, and average forecasts across all observations. The value of the average 
forecast is for the y-axis. In a two-way partial dependence plot, for each value combination of two predictors (in 
both the x-axis and y-axis), we force all observations in the training sample to assume the value combination for the 
two predictors without changing any data points for other predictors, compute the forecasts using the chosen model, 
and average forecasts across all observations. The value of the average forecast is coded by color. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 38

36 
 
interaction effect: 𝑃𝑃𝑃𝑃
෢ becomes lower when both the tax benefits and lagged retained earnings 
(the top 1 predictor under stochastic gradient boosting) are higher. The results are consistent with 
Bartov and Mohanram’s [2004] finding that the exercise of executive stock options predicts 
disappointing earnings and reveals management’s private information on the reversal of 
previously inflated earnings. 
 
6. Conclusion 
We apply machine learning to a large set of detailed financial information aimed at 
predicting the direction of future earnings changes. Leveraging ensemble learning methods 
(random forests and stochastic gradient boosting), we combine the detailed financial data into a 
summary measure for the direction of one-year-ahead earnings changes. The measure shows 
significant out-of-sample predictive power concerning the direction of earnings changes. The 
AUC ranges from 67.52 to 68.66 percent and is significantly higher than that of a random guess, 
which is only 50 percent. The annual size-adjusted returns to hedge portfolios formed based on 
this measure range from 5.02 to 9.74 percent, indicating economically significant predictive 
gains. 
Our models using machine learning and a large set of detailed financial information 
outperform two conventional models that use logistic regressions and small sets of accounting 
variables, and professional analysts’ forecasts. Analyses suggest that the outperformance relative 
to the conventional models stems from both nonlinear predictor interactions missed by 
regressions and the use of more detailed financial data. The results highlight the usefulness of 
machine learning and detailed financial information in predicting the direction of earnings 
changes. 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 39

37 
 
REFERENCES 
 
ANAND, V.; R. BRUNNER; K. IKEGWU; AND T. SOUGIANNIS. “Predicting Profitability Using 
Machine Learning.” Working Paper, University of Illinois at Urbana-Champaign, 2019. 
Available at https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3466478.  
BAO, Y.; B. KE; B. LI; J. YU; AND J. ZHANG. “Detecting Accounting Fraud in Publicly Traded US 
Firms Using a Machine Learning Approach.” Journal of Accounting Research 58 (2020): 
199-235. 
BARTH, M.; K. LI; AND C. MCCLURE. “Evolution in Value Relevance of Accounting 
Information.” Working Paper, Stanford University, 2021. Available at 
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2933197. 
BARTLEY, J.; A. CHEN; AND E. TAYLOR. “A Comparison of XBRL Filings to Corporate 10-Ks—
Evidence from the Voluntary Filing Program.” Accounting Horizon 25 (2011): 227-245. 
BARTOV, E., AND P. MOHANRAM. “Private Information, Earnings Manipulations, and Executive 
Stock-option Exercises.” The Accounting Review 79 (2004): 889-920. 
BENTLEY, J.; T. CHRISTENSEN; K. GEE; AND B. WHIPPLE. “Disentangling Managers’ and 
Analysts’ Non-GAAP Reporting.” Journal of Accounting Research 56 (2018): 1039-
1081. 
BERTOMEU, J.; E. CHEYNEL; E. FLOYD; AND W. PAN. “Using Machine Learning to Detect 
Misstatements.” Review of Accounting Studies 26 (2021): 468-519. 
BHATTACHARYA, N.; Y. CHO; AND J. KIM. “Leveling the Playing Field between Large and Small 
Institutions: Evidence from the SEC’s XBRL Mandate.” The Accounting Review 93 
(2018): 51-71. 
BINZ, O.; K. SCHIPPER; AND K. STANDRIDGE. “What can Analysts Learn from Artificial 
Intelligence about Fundamental Analysis?” Working Paper, INSEAD, Duke University, 
2021. Available at https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3745078. 
BLANKESPOOR, E. “The Impact of Information Processing Costs on Firm Disclosure Choice: 
Evidence from the XBRL Mandate.” Journal of Accounting Research 57 (2019): 919-
967. 
BLANKESPOOR, E.; B. MILLER; AND H. WHITE. “Initial Evidence on the Market Impact of the 
XBRL Mandate.” Review of Accounting Studies 19 (2014): 1468-1503. 
BORITZ, E., AND W. NO. “Assurance on XBRL-related Documents: The Case of United 
Technologies Corporation.” Journal of Information Systems 23 (2009): 49-78. 
BRADSHAW, M.; T. CHRISTENSEN; K. GEE; AND B. WHIPPLE. “Analysts’ GAAP Earnings 
Forecasts and their Implications for Accounting Research.” Journal of Accounting and 
Economics 66 (2018): 46-66. 
BREIMAN, L. “Random Forests.” Machine Learning 45 (2001): 5-32. 
BROWN, L. “Influential Accounting Articles, Individuals, Ph.D. Granting Institutions and 
Faculties: A Citational Analysis.” Accounting, Organization and Society 21 (1996): 723-
754. 
CAO, K., AND H. YOU. “Fundamental Analysis via Machine Learning.” Working Paper, Hong 
Kong University of Science and Technology, 2020. Available at 
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3706532. 
CARPENTER, J., AND J. BITHELL. “Bootstrap Confidence Intervals: When, Which, What? A 
Practical Guide for Medical Statisticians.” Statistics in Medicine 19 (2000): 1141-1164. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 40

38 
 
CECCHINI, M.; H. AYTUG; G. KOEHLER; AND P. PATHAK. “Detecting Management Fraud in Public 
Companies.” Management Science 56 (2010): 1146-1160. 
CHINCO, A.; A. CLARK-JOSEPH; AND M. YE. “Sparse Signals in the Cross-Section of Returns.” 
Journal of Finance 74 (2019): 449-492. 
CHYCHYLA, R., AND A. KOGAN. “Using XBRL to Conduct a Large-Scale Study of Discrepancies 
between the Accounting Numbers in Compustat and SEC 10-K Filings.” Journal of 
Information Systems 29 (2015): 37-72. 
COHEN, L.; C. MALLOY; AND Q. NGUYEN. “Lazy Prices.” Journal of Finance 75 (2020): 1371-
1415. 
DE FRANCO, G.; M.H. WONG; AND Y. ZHOU. “Accounting Adjustments and the Valuation of 
Financial Statement Note Information in 10-K Filings.” The Accounting Review 86 
(2011): 1577-1604. 
DEBRECENY, R.; S. FAREWELL; M. PIECHOCKI; C. FELDEN; AND A. GRANING. “Does It Add Up? 
Early Evidence on the Data Quality of XBRL Filings to the SEC.” Journal of Accounting 
and Public Policy 29 (2010): 296-306. 
DEBRECENY, R.; S. FAREWELL; M. PIECHOCKI; C. FELDEN; A. GRANING; AND A. D’ERI. “Flex or 
Break? Extensions in XBRL Disclosures to the SEC.” Accounting Horizon 25 (2011): 
631-657. 
DING, K.; B. LEV; X. PENG; T. SUN; AND M. VASARHELYI. “Machine Learning Improves 
Accounting Estimates: Evidence from Insurance Payments.” Review of Accounting 
Studies 25 (2020): 1098-1134. 
DONG, Y.; O. LI; Y. LIN; AND C. NI. “Does Information Processing Cost Affect Firm-Specific 
Information Acquisition? Evidence from XBRL Adoption.” Journal of Financial and 
Quantitative Analysis 51 (2016): 435-462. 
DU, H.; M. VASARHELYI; AND X. ZHENG. “XBRL Mandate: Thousands of Filing Errors and So 
What?” Journal of Information Systems 27 (2013): 61-78. 
DYER, T.; M. LANG; AND L. STICE-LAWRENCE. “The Evolution of 10-K Textual Disclosure: 
Evidence from Latent Dirichlet Allocation.” Journal of Accounting and Economics 64 
(2017): 221-245. 
EFENDI, J.; J. PARK; AND C. SUBRAMANIAM. “Does the XBRL Reporting Format Provide 
Incremental Information Value? A Study Using XBRL Disclosures during the Voluntary 
Filing Program.” Abacus 52 (2016): 259-285. 
FAMA, E., AND K. FRENCH. “A Five-Factor Asset Pricing Model.” Journal of Financial 
Economics 116 (2015): 1-22. 
FASB. SEC Reporting Taxonomy Technical Guide. 2018. Available at 
https://www.fasb.org/cs/ContentServer?d=Touch&c=Document_C&pagename=FASB%
2FDocument_C%2FDocumentPage&cid=1176169716122. 
FAWCETT, T. “An Introduction to ROC Analysis.” Pattern Recognition Letters 27 (2006): 861-
874. 
FRANKEL, R.; J. JENNINGS; AND J. LEE. “Using Unstructured and Qualitative Disclosures to 
Explain Accruals.” Journal of Accounting and Economics 62 (2016): 209-227. 
FREEMAN, R.; J. OHLSON; AND S. PENMAN. “Book Rate-of-Return and Prediction of Earnings 
Changes: An Empirical Investigation.” Journal of Accounting Research 20 (1982): 639-
653. 
FREYBERGER, J.; N. ANDREAS; AND M. WEBER. “Dissecting Characteristics Nonparametrically.” 
Review of Financial Studies 33 (2020): 2326-2377. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 41

39 
 
FRIEDMAN, J. “Stochastic Gradient Boosting.” Computational Statistics & Data Analysis 38 
(2002): 367-378. 
GERAKOS, J., AND R. GRAMACY. “Regression-Based Earnings Forecasts.” Chicago Booth 
Research Paper No. 12-26, 2013. 
GREEN, J.; J. HAND; AND M. SOLIMAN. “Going, Going, Gone? The Apparent Demise of the 
Accruals Anomaly.” Management Science 57 (2011): 797-816. 
GU, S.; B. KELLY; AND D. XIU. “Empirical Asset Pricing via Machine Learning.” Review of 
Financial Studies 33 (2020): 2223-2273. 
HANLON, M. “The Persistence and Pricing of Earnings, Accruals, and Cash Flows When Firms 
Have Large Book-Tax Differences.” The Accounting Review 80 (2005): 137-166. 
HANLON, M., AND T. SHEVLIN. “Accounting for Tax Benefits of Employee Stock Options and 
Implications for Research.” Accounting Horizon 16 (2002): 1-16. 
HARRIS, T., AND S. MORSFIELD. “An Evaluation of the Current State and Future of XBRL and 
Interactive Data for Investors and Analysts.” White Paper, Columbia University, 2012. 
HASTIE, T.; R. TIBSHIRANI; AND J. FRIEDMAN. The Elements of Statistical Learning: Data 
Mining, Inference, and Prediction. 2nd Edition. New York: Springer, 2009. 
HOLTHAUSEN, R., AND D. LARCKER. “The Prediction of Stock Returns Using Financial Statement 
Information.” Journal of Accounting and Economics 15 (1992): 373-411. 
HOU, K.; C. XUE; AND L. ZHANG. “Replicating Anomalies.” Review of Financial Studies 33 
(2020): 2019-2133.  
HSIEH, T.; AND J. BEDARD. “Impact of XBRL on Voluntary Adopters’ Financial Reporting 
Quality and Cost of Equity Capital.” Journal of Emerging Technologies in Accounting 15 
(2018): 45-65. 
HUNT, J.; J. MYERS; AND L. MYERS. “Improving Earnings Predictions with Machine Learning.” 
Working Paper, Mississippi State University, 2019. 
JAPKOWICZ, N., AND S. STEPHEN. “The Class Imbalance Problem: A Systematic Study.” 
Intelligent Data Analysis 6 (2002): 429-449. 
KIM, J.; B. LI; AND Z. LIU. “Information-Processing Costs and Breadth of Ownership.” 
Contemporary Accounting Research 36 (2019a): 2408-2436. 
KIM, J.; J. KIM; AND J. LIM. “Does XBRL Adoption Constrain Earnings Management? Early 
Evidence from Mandated US Filers.” Contemporary Accounting Research 36 (2019b): 
2610-2634. 
KIRK, M.; J. VINCENT; AND D. WILLIAMS. “From Print to Practice: XBRL Extension Use and 
Analyst Forecast Properties.” Working Paper, University of Florida, and University of 
Illinois, 2016. Available at https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2826159. 
KOTHARI, S.P. “Capital Markets Research in Accounting.” Journal of Accounting and 
Economics 31 (2001): 105-231. 
LEV, B., AND F. GU. The End of Accounting and the Path Forward for Investors and Managers. 
New Jersey: John Wiley & Sons, Inc., 2016. 
LEV, B., AND D. NISSIM. “Taxable Income, Future Earnings, and Equity Values.” The Accounting 
Review 79 (2004): 1039-1074. 
LI, F. “The Information Content of Forward-Looking Statements in Corporate Filings – A Naïve 
Bayesian Machine Learning Approach.” Journal of Accounting Research 48 (2010): 
1049-1102. 
LI, K., AND P. MOHANRAM. “Evaluating Cross-Sectional Forecasting Models for Implied Cost of 
Capital.” Review of Accounting Studies 19 (2014): 1152-1185. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 42

40 
 
LI, K., AND P. MOHANRAM. “Fundamental Analysis: Combining the Search for Quality with the 
Search for Value.” Contemporary Accounting Research 36 (2019): 1263-1298. 
LI, S., AND E. NWAEZE. “The Association between Extensions in XBRL Disclosures and 
Financial Information Environment.” Journal of Information Systems 29 (2015): 73-99. 
LI, S., AND E. NWAEZE. “Impact of Extensions in XBRL Disclosure on Analysts’ Forecast 
Behavior.” Accounting Horizon 32 (2018): 57-79. 
LIU, M. “Assessing Human Information Processing in Lending Decisions: A Machine Learning 
Approach.” Working Paper, Boston University, 2021. Available at 
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3483699. 
LIVNAT, J., AND J. SINGH. “Machine Learning Algorithms to Classify Future Returns Using 
Structured and Unstructured Data.” Journal of Investing 30 (2021): 62-78. 
MILLER, G., AND D. SKINNER. “Determinants of the Valuation Allowance for Deferred Tax 
Assets under SFAS No. 109.” The Accounting Review 73 (1998): 213-233. 
MONAHAN, S. “Financial Statement Analysis and Earnings Forecasting.” Foundations and 
Trends in Accounting 12 (2018): 105-215. 
MULLAINATHAN, S., AND J. SPIESS. “Machine Learning: An Applied Econometric Approach.” 
Journal of Economic Perspectives 31 (2017): 87-106. 
NISSIM, D., AND S. PENMAN. “Ratio Analysis and Equity Valuation: From Research to Practice.” 
Review of Accounting Studies 6 (2001): 109-154. 
NOVY-MARX, R., AND M. VELIKOV. “A Taxonomy of Anomalies and Their Trading Costs.” 
Review of Financial Studies 29 (2016): 104-147. 
OU, J. “The Information Content of Nonearnings Accounting Numbers as Earnings Predictors.” 
Journal of Accounting Research 28 (1990): 144-163. 
OU, J. AND S. PENMAN. “Financial Statement Analysis and the Prediction of Stock Returns.” 
Journal of Accounting and Economics 11 (1989): 295-329. 
PEDREGOSA, F.; G. VAROQUAUX; A. GRAMFORT; V. MICHEL; B. THIRION; O. GRISEL; M. 
BLONDEL; P. PRETTENHOFER; R. WEISS; V. DUBOURG; J. VANDERPLAS; A. PASSOS; D. 
COURNAPEAU; M. BRUCHER; M. PERROT; AND E. DUCHESNAY. “Scikit-learn: Machine 
Learning in Python.” Journal of Machine Learning Research 12 (2011): 2825-2830. 
Available at https://scikit-
learn.org/stable/auto_examples/inspection/plot_permutation_importance_multicollinear.html 
PEROLS, J. “Financial Statement Fraud Detection: An Analysis of Statistical and Machine 
Learning Algorithms.” Auditing: A Journal of Practice & Theory 30 (2011): 19-50. 
PIOTROSKI, J., AND E. SO. “Identifying Expectation Errors in Value/Glamour Strategies: A 
Fundamental Analysis Approach.” Review of Financial Studies 25 (2012): 2841-2875. 
PLUMLEE, R.D., AND M. PLUMLEE. “Assurance on XBRL for Financial Reporting.” Accounting 
Horizon 22 (2008): 353-368. 
RASEKHSCHAFFE, K., AND R. JONES. “Machine Learning for Stock Selection.” Financial Analysts 
Journal 75 (2019): 70-88. 
RICHARDSON, S.; I. TUNA; AND P. WYSOCKI. “Accounting Anomalies and Fundamental Analysis: 
A Review of Recent Research Advances.” Journal of Accounting and Economics 50 
(2010): 410-454. 
SEC. Interactive Data to Improve Financial Reporting. Final rule. 2009. Available at 
https://www.sec.gov/rules/final/2009/33-9002.pdf 
SEC. Staff Observations of Custom Axis Tags. 2016. Available at 
https://www.sec.gov/structureddata/reportspubs/osd_assessment_custom-axis-tags.html 
SHUMWAY, T. “The Delisting Bias in CRSP Data.” Journal of Finance 52 (1997): 327-340. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 43

41 
 
SHUMWAY, T., AND V. WARTHER. "The Delisting Bias in CRSP’s Nasdaq Data and Its 
Implications for the Size Effect.” Journal of Finance 54 (1999): 2361-2379. 
SOLIMAN, M. “The Use of DuPont Analysis by Market Participants.” The Accounting Review 83 
(2008): 823-853. 
THOMAS, J., AND F. ZHANG. “Tax Expense Momentum.” Journal of Accounting Research 49 
(2011): 791-821. 
VARIAN, H. “Big data: New Tricks for Econometrics.” Journal of Economic Perspectives 28 
(2014): 3-28. 
WAHLEN, J., AND M. WIELAND. “Can Financial Statement Analysis Beat Consensus Analysts’ 
Recommendations?” Review of Accounting Studies 16 (2011): 89-115. 
ZHOU, Z. Ensemble Learning Methods: Foundations and Algorithms. Florida: CRC Press, 2012. 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 44

42 
 
APPENDIX A: EXAMPLES OF XBRL-TAGGED FINANCIAL ITEMS 
 
This appendix shows where an XBRL document is filed and how financial items are tagged in the XBRL document. 
The following screenshot shows where a human-readable HTML document and the corresponding machine-readable 
XBRL document are located on the SEC EDGAR Website for Littelfuse, an electronic manufacturer. 
 
 
 
Example 1: Items on the face of financial statements 
Cash and cash equivalents from the human-readable HTML document: 
 
Cash and cash equivalents from the machine-readable XBRL document: 
 
 
 
Human-readable 
HTML document 
Machine-readable 
XBRL document 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 45

43 
 
 
Example 2: Items in the footnotes 
Work in process inventory from the human-readable HTML document: 
 
 
Work in process inventory from the machine-readable XBRL document: 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 46

44 
 
 
 
 
 
 
 
 
 
 
 
 
 
FIG 1. – An Example of a Decision Tree to Predict an Earnings Increase. In this figure, the left panel presents an example of a decision tree to predict a one-year-
ahead earnings increase. A blue box (“a node”) represents a split and a green box (“a leaf”) indicates a final partition. The right panel shows how the space of 
“EPS” and “Lev” is partitioned by this tree model. See Section 2.1 for details.  
 
Lev < 0.7 
Group 1 
EPS > 0.5 
Group 2 
Group 3 
True 
False 
True 
False 
EPS    0.5 
Lev      0.7                     1 
0 
1 
Group 1 
Group 2 
Group 3 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 47

45 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
FIG 2. – Random Forests. This figure shows how an ensemble prediction of the probability of an earnings increase (𝑃𝑃𝑃𝑃
෢) is generated by random forests. See 
Section 2.1 for details. 
 
Bootstrap sample 1 with random 
𝑘𝑘 variables for each split  
Bootstrap sample 2 with random 
𝑘𝑘 variables for each split 
Bootstrap sample m with random 
𝑘𝑘 variables for each split 
 
 
 
 
 
 
 
 
 
 
 
 
 
• • • 
 
 
Ensemble prediction 𝑃𝑃𝑃𝑃
෢= 𝑀𝑀𝑀𝑀𝑀𝑀𝑀𝑀(𝑦𝑦ො1 , 𝑦𝑦ො2 , …, 𝑦𝑦ො𝑚𝑚) 
 𝑦𝑦ො1 
 𝑦𝑦ො2 
 𝑦𝑦ො𝑚𝑚 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 48

46 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
FIG 3. – Stochastic Gradient Boosting. This figure shows how an ensemble prediction of the probability of an earnings increase (𝑃𝑃𝑃𝑃
෢) is generated by stochastic 
gradient boosting. See Section 2.1 for details. 
 
Random sample 1 to forecast 
residuals from 𝑃𝑃𝑃𝑃0
෢, which yield 𝛾𝛾0
ෞ 
Random sample 2 to forecast 
residuals from 𝑃𝑃𝑃𝑃1
෢, which yield 𝛾𝛾1ෝ  
Random sample m to forecast residuals 
from 𝑃𝑃𝑃𝑃𝑚𝑚−1
෣, which yield 𝛾𝛾𝑚𝑚−1
ෟ 
 
 
 
 
 
 
 
 
 
• • • 
 𝐹𝐹0(𝑥𝑥) = log ቀ
#𝑜𝑜𝑜𝑜 𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼𝐼
#𝑜𝑜𝑜𝑜 𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷𝐷ቁ       𝐹𝐹1(𝑥𝑥) = 𝐹𝐹0(𝑥𝑥) +  𝜌𝜌× 𝛾𝛾0
ෞ                      𝐹𝐹2(𝑥𝑥) = 𝐹𝐹1(𝑥𝑥) +  𝜌𝜌× 𝛾𝛾1ෝ                                                         𝐹𝐹𝑚𝑚(𝑥𝑥) = 𝐹𝐹𝑚𝑚−1(𝑥𝑥) +  𝜌𝜌× 𝛾𝛾𝑚𝑚−1
ෟ 
Ensemble prediction  𝑃𝑃𝑃𝑃
෢=
𝑒𝑒𝐹𝐹𝑚𝑚(𝑥𝑥)
1+𝑒𝑒𝐹𝐹𝑚𝑚(𝑥𝑥) 
 𝑃𝑃𝑃𝑃0
෢=
𝑒𝑒𝐹𝐹0(𝑥𝑥)
1+𝑒𝑒𝐹𝐹0(𝑥𝑥)                                  𝑃𝑃𝑃𝑃1
෢=
𝑒𝑒𝐹𝐹1(𝑥𝑥)
1+𝑒𝑒𝐹𝐹1(𝑥𝑥)                                         𝑃𝑃𝑃𝑃2
෢=
𝑒𝑒𝐹𝐹2(𝑥𝑥)
1+𝑒𝑒𝐹𝐹2(𝑥𝑥)         
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 49

47 
 
 
Panel A: Histogram by the number of distinct tags 
Panel B: Histogram by the proportion of custom 
tags 
 
Panel C: Histogram by the proportion of uncommon 
standard tags 
 
Panel D: Histogram by the number of common 
standard tags 
 
 
FIG 4. – Tag Distribution Across XBRL Submissions. Frequency refers to the proportion of XBRL documents in the 
8,149 submissions. Panel A shows the histogram by the number of total distinct tags (including both custom and 
standard tags). Panel B shows the histogram by the proportion of custom tags, calculated as the number of distinct 
custom tags divided by the number of distinct tags. Panel C shows the histogram by the proportion of uncommon 
standard tags, calculated as the number of distinct uncommon standard tags divided by the number of distinct tags. 
Uncommon standard tags are standard tags that have not been used at least once in each year. Panel D shows the 
histogram by the number of common standard tags, which are standard tags that have been used at least once in each 
year. We use 4,627 distinct common standard tags in subsequent analyses.  
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 50

48 
 
 
Panel A: Using detailed financial data from XBRL 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 51

49 
 
Panel B: Using detailed financial data from Compustat 
 
 
 
FIG 5. – Comparison of Out-of-sample ROC curves for Different Data/Method Combinations. Panels A and B present 
out-of-sample ROC curves for different data/method combinations. The data consist of Ou and Penman’s [1989] 
variables (OP), DuPont variables, our XBRL-tagged items (XBRL), and all Compustat items (COM). The employed 
methods are logistic regression (Logit), random forests (RF), and stochastic gradient boosting (SGB). Analysts’ 
forecasts are taken from I/B/E/S. 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 52

50 
 
Panel A: Group importance for random forests 
 
Panel B: Group importance for stochastic gradient boosting 
 
Fig 6. – Group Importance. Panels A and B show the importance of predictors grouped by financial statement 
category for random forests and stochastic gradient boosting, respectively. Each predictor is classified into 
balance sheet, income statement, comprehensive income statement, cash flow statement, shareholders’ equity 
statement, or footnotes. The importance of a predictor is computed as the decrease in the predictive performance 
when that variable is randomly shuffled. The sum and mean of the predictor importance grouped by financial 
statement category are reported. 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 53

51 
 
Panel A: Valuation allowance for deferred tax 
assets (random forests) 
Panel B: Valuation allowance for deferred tax assets 
and operating income (random forests) 
 
 
 
Panel C: Tax benefits of share-based compensation 
(stochastic gradient boosting) 
 
Panel D: Tax benefits of share-based compensation 
and retained earnings (stochastic gradient boosting) 
 
 
 
Fig 7. – Partial Dependence Plots. Panels A and C show one-way partial dependence plots and Panels B and D show 
two-way partial dependence plots. In a one-way partial dependence plot, for each value of a predictor (in the x-axis), 
we force all observations in the training sample to assume that value for that predictor without changing any data 
points for other predictors, compute the forecasts using the chosen model, and average forecasts across all 
observations. The value of the average forecast is for the y-axis. In a two-way partial dependence plot, for each 
value combination of two predictors (in both the x-axis and y-axis), we force all observations in the training sample 
to assume the value combination for those two predictors without changing any data points for other predictors, 
compute the forecasts using the chosen model, and average forecasts across all observations. The value of the 
average forecast is coded by color. 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 54

52 
 
TABLE 1 
Sample Selection 
 
 
 
Number of submissions 
(1) XBRL filings for 10-K and 10-K/A between June 15, 2012 and March 31, 2018 that 
can be matched to pro forma earnings from I/B/E/S 
10,073 
(2) Requiring stock price data available from CRSP 
8,381 
(3) Requiring non-zero total assets 
8,358 
(4) Retaining the most recent XBRL filings as of three months after the fiscal year end     
8,149 
 
This table shows the sample selection procedure. We start our sample with XBRL filings for 10-K and 10-K/A between 
June 15, 2012 and March 31, 2018 that can be matched to pro forma earnings from I/B/E/S. To ensure compliance with 
mandatory footnote disclosure in the XBRL format, we require that an XBRL filing has a reporting period ending on or 
after June 15, 2012. We require a filing to have stock price data available from CRSP and non-zero total assets. 
Exploiting the point-in-time nature of XBRL-tagged financial data, we only retain the most recent filings as of three 
months after the fiscal year end. 
 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 55

53 
 
TABLE 2 
Sample Distribution 
 
Panel A: XBRL filings by calendar period  
Calendar Period 
Number of submissions 
2012Q3-2012Q4 
119 
2013Q1-2013Q4 
1,206 
2014Q1-2014Q4 
1,304 
2015Q1-2015Q4 
1,375 
2016Q1-2016Q4 
1,371 
2017Q1-2017Q4 
1,460 
2018Q1 
1,314 
Total 
8,149 
 
Panel B: XBRL filings by industry 
Industry 
Number of submisssions 
Food Products 
149 
Beer & Liquor 
23 
Tobacco Products 
21 
Recreation 
79 
Printing and Publishing 
66 
Consumer Goods 
121 
Apparel 
94 
Healthcare, Medical Equipment, Pharmaceutical Products 
588 
Chemicals 
205 
Textiles 
26 
Construction and Construction Materials 
210 
Steel Works 
102 
Fabricated Products and Machinery 
303 
Electrical Equipment 
87 
Automobiles and Trucks 
170 
Aircraft, ships, and railroad equipment 
56 
Precious Metals, Non-Metallic, and Industrial Metal Mining 
92 
Coal 
10 
Petroleum and Natural Gas 
390 
Utilities 
248 
Communication 
165 
Personal and Business Services 
1,353 
Business Equipment 
995 
Business Supplies and Shipping Containers 
126 
Transportation 
194 
Wholesale 
202 
Retail 
374 
Restaurants, Hotels, Motels 
222 
Banking, Insurance, Real Estate, Trading 
1,344 
Other 
134 
Total 
8,149 
 
Panel A shows the number of XBRL filings by year for the final sample of 8,149 filings. Panel B provides the 
number of XBRL filings by Fama-French 30-industry classification. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 56

54 
 
TABLE 3  
Summary Statistics 
 
Panel A: Number of predictors by financial statement category 
Financial Statement Category 
Number of Current Predictors 
Number of Lagged Predictors 
Number of %∆ Predictors 
Total 
Balance Sheet 
639 
639 
639 
1,917 
Income Statement 
740 
740 
740 
2,220 
Cash Flow Statement 
87 
87 
87 
261 
Comprehensive Income Statement 
131 
131 
131 
393 
Shareholders’ Equity Statement 
587 
587 
587 
1,761 
Footnotes 
2,443 
2,443 
2,443 
7,329 
Total 
4,627 
4,627 
4,627 
13,881 
 
Panel B: Top 10 most populated current predictors (i.e., non-zero values) from Balance Sheet 
Predictor 
Frequency 
Mean 
Q1 
Median 
Q3 
Assets t 
8,149 
17,281.54 
648.90 
2,247.50 
7,739.48 
LiabilitiesAndStockholdersEquity t 
8,138 
17,290.44 
648.93 
2,245.22 
7,730.65 
RetainedEarningsAccumulatedDeficit t 
7,882 
2,672.38 
-71.66 
195.64 
1,402.41 
CashAndCashEquivalentsAtCarryingValue t 
7,836 
762.66 
43.25 
133.65 
479.34 
PropertyPlantAndEquipmentNet t 
7,641 
2,638.33 
47.47 
219.90 
1,035.82 
StockholdersEquity t 
7,635 
3,699.93 
219.88 
702.92 
2,282.85 
AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment t 
7,349 
2,052.62 
50.78 
238.90 
956.60 
CommonStockSharesAuthorized t 
7,066 
142,369.72 
100.00 
210.00 
500.00 
AccumulatedOtherComprehensiveIncomeLossNetOfTax t 
7,022 
-297.63 
-107.91 
-8.58 
-0.01 
PropertyPlantAndEquipmentGross t 
6,935 
4,659.43 
108.59 
485.09 
1,982.24 
 
Panel C: Top 10 most populated current predictors (i.e., non-zero values) from Income Statement 
Predictor 
Frequency 
Mean 
Q1 
Median 
Q3 
IncomeTaxExpenseBenefit t 
7,903 
153.03 
0.75 
18.73 
96.02 
WeightedAverageNumberOfSharesOutstandingBasic t 
7,312 
316.50 
32.75 
66.16 
165.82 
WeightedAverageNumberOfDilutedSharesOutstanding t 
7,292 
306.07 
33.23 
68.00 
169.64 
NetIncomeLoss t 
7,280 
314.61 
-0.77 
30.80 
164.65 
EarningsPerShareBasic t 
7,227 
1.28 
0.10 
0.75 
2.02 
EarningsPerShareDiluted t 
7,210 
1.20 
0.08 
0.70 
1.92 
OperatingIncomeLoss t 
6,669 
475.35 
3.61 
66.28 
310.50 
AmortizationOfIntangibleAssets t 
5,890 
76.53 
2.80 
11.84 
37.86 
InterestExpense t 
5,672 
162.74 
5.99 
29.78 
107.64 
IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments t 
4,794 
493.18 
4.64 
71.61 
329.66 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 57

55 
 
Panel D: Top 10 most populated current predictors (i.e., non-zero values) from Cash Flow Statement 
Predictor 
Frequency 
Mean 
Q1 
Median 
Q3 
DeferredIncomeTaxExpenseBenefit t 
7,252 
105.66 
-12.28 
-0.04 
11.87 
CashAndCashEquivalentsPeriodIncreaseDecrease t 
7,166 
20.01 
-26.00 
2.86 
51.53 
ShareBasedCompensation t 
6,939 
45.94 
4.88 
13.44 
36.00 
PaymentsToAcquirePropertyPlantAndEquipment t 
6,041 
307.97 
9.89 
39.36 
148.60 
NetCashProvidedByUsedInInvestingActivities t 
5,672 
-705.14 
-507.93 
-115.04 
-20.38 
NetCashProvidedByUsedInOperatingActivities t 
5,647 
997.09 
44.45 
175.49 
660.75 
NetCashProvidedByUsedInFinancingActivities t 
5,626 
-267.87 
-194.89 
-15.86 
59.51 
IncreaseDecreaseInAccountsReceivable t 
5,063 
36.00 
-2.68 
5.30 
28.23 
Depreciation t 
4,963 
201.04 
10.33 
34.20 
113.19 
DepreciationDepletionAndAmortization t 
4,943 
351.03 
16.66 
59.63 
197.85 
 
Panel E: Top 10 most populated current predictors (i.e., non-zero values) from Comprehensive Income Statement 
Predictor 
Frequency 
Mean 
Q1 
Median 
Q3 
ComprehensiveIncomeNetOfTax t 
6,899 
450.00 
-1.73 
56.98 
276.68 
OtherComprehensiveIncomeLossNetOfTax t 
4,616 
-27.54 
-30.69 
-0.60 
10.00 
OtherComprehensiveIncomeLossForeignCurrencyTransactionAndTranslationAdjustmentNetOfTax t 
3,554 
-47.83 
-25.10 
-1.23 
1.90 
ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest t 
3,206 
769.20 
14.39 
139.37 
584.73 
OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent t 
2,384 
-33.09 
-18.90 
-0.42 
6.62 
OtherComprehensiveIncomeLossPensionAndOtherPostretirementBenefitPlansAdjustmentNetOfTax t 
2,235 
-13.37 
-9.37 
-0.02 
9.00 
ComprehensiveIncomeNetOfTaxAttributableToNoncontrollingInterest t 
2,209 
38.64 
-0.12 
2.20 
20.00 
OtherComprehensiveIncomeUnrealizedHoldingGainLossOnSecuritiesArisingDuringPeriodNetOfTax t 
2,038 
3.94 
-0.60 
0.00 
1.00 
OtherComprehensiveIncomeUnrealizedGainLossOnDerivativesArisingDuringPeriodNetOfTax t 
1,666 
0.96 
-2.20 
0.06 
2.84 
OtherComprehensiveIncomeLossDerivativesQualifyingAsHedgesNetOfTax t 
1,491 
-0.01 
-2.00 
0.20 
3.67 
 
Panel F: Top 10 most populated current predictors (i.e., non-zero values) from Shareholders’ Equity Statement 
Predictor 
Frequency 
Mean 
Q1 
Median 
Q3 
CommonStockSharesOutstanding t 
5,529 
97,794.45 
31.99 
60.39 
146.21 
AdjustmentsToAdditionalPaidInCapitalSharebasedCompensationRequisiteServicePeriodRecognitionValue t 
4,786 
39.81 
4.77 
13.00 
31.90 
TreasuryStockValue t 
4,087 
2,296.51 
22.90 
190.00 
1,107.70 
StockIssuedDuringPeriodSharesStockOptionsExercised t 
3,854 
413.75 
0.10 
0.44 
1.30 
StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest t 
3,782 
6,379.97 
490.33 
1,378.19 
4,630.00 
StockIssuedDuringPeriodValueStockOptionsExercised t 
2,774 
17.33 
0.63 
3.18 
11.70 
CommonStockDividendsPerShareDeclared t 
2,609 
0.98 
0.30 
0.66 
1.28 
TreasuryStockValueAcquiredCostMethod t 
2,319 
1,213.40 
7.47 
58.95 
319.33 
StockIssuedDuringPeriodValueShareBasedCompensation t 
2,120 
47.01 
1.00 
6.97 
29.26 
DividendsCommonStockCash t 
2,035 
316.86 
17.02 
62.93 
198.00 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 58

56 
 
 
Panel G: Top 10 most populated current predictors (i.e., non-zero values) from Footnotes 
Predictor 
Frequency 
Mean 
Q1 
Median 
Q3 
OperatingLeasesFutureMinimumPaymentsDueInTwoYears t 
7,091 
64.62 
4.10 
12.91 
42.96 
OperatingLeasesFutureMinimumPaymentsDueCurrent t 
7,062 
73.22 
4.87 
15.46 
50.29 
OperatingLeasesFutureMinimumPaymentsDueInThreeYears t 
7,060 
143.75 
3.31 
10.60 
35.33 
OperatingLeasesFutureMinimumPaymentsDueInFourYears t 
6,935 
46.65 
2.68 
8.80 
29.00 
OperatingLeasesFutureMinimumPaymentsDueInFiveYears t 
6,570 
40.10 
2.30 
7.48 
25.00 
CurrentStateAndLocalTaxExpenseBenefit t 
6,371 
14.23 
0.17 
1.59 
7.20 
CurrentIncomeTaxExpenseBenefit t 
6,347 
285.83 
2.55 
20.40 
90.52 
DeferredFederalIncomeTaxExpenseBenefit t 
6,310 
11.73 
-8.69 
0.28 
13.00 
OperatingLeasesFutureMinimumPaymentsDue t 
6,309 
428.10 
20.96 
70.00 
254.30 
OperatingLeasesFutureMinimumPaymentsDueThereafter t 
6,288 
202.01 
5.87 
25.00 
97.00 
 
Panel A shows the number of current predictors, lagged predictors, and percentage changes by financial statement category. Panels B, C, D, E, F, and G 
provide lists of the top 10 most populated (i.e., non-zero) current predictors from balance sheet, income statement, cash flow statement, comprehensive income 
statement, shareholders’ equity statement, and footnotes, respectively, and descriptive statistics for the predictor values. Frequency counts the number of 
XBRL filings with a non-zero predictor value. Except for per share items, all predictor values are in millions. 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 59

57 
 
TABLE 4  
Parameters for Machine Learning 
 
Parameters 
Random forests 
Stochastic gradient boosting 
# of variables (k) 
From 110 to 120 
 
# of trees (m) 
500, 600, 700,... 2000 
500, 600, 700,…, 2000 
Learning rate (ρ) 
 
0.005, 0.01, 0.05 
Tree depth (L) 
 
1, 2, 3, 4 
Min. # of obs. in a leaf (b) 
1, 2, 3, 4 
10 
Bagging 
0.5 
0.5 
 
This table presents the parameter values considered in training the respective machine 
learning model. # of variables (k) is the number of variables (i.e., predictors) to be randomly 
selected when forming a split in a tree. # of trees (m) is the number of trees to be grown. 
Learning rate (ρ) is the extent to which each tree iteration contributes to the base tree. Tree 
depth (L) is the maximum depth of each tree. Min. # of obs. in a leaf (b) is the minimum 
number of observations in the terminal nodes of each tree. Bagging is the fraction of 
observations to be randomly selected to grow a tree. 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 60

58 
 
TABLE 5  
Out-of-sample Prediction Performance 
 
Panel A: Machine learning models 
 
Random forests 
Stochastic gradient boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
Number of observations 
5,520 
3,338 
5,520 
3,649 
Number of earnings increases 
2,552 
1,362 
2,552 
1,547 
Number of earnings decreases 
2,968 
1,976 
2,968 
2,102 
 
 
 
 
 
% of predicted increases that are actual earnings increases 
60.10 
65.64 
60.05 
64.50 
% correctly predicted 
61.90 
67.50 
62.26 
66.90 
% of actual earnings increases correctly predicted 
33.07 
28.12 
31.03 
29.28 
% of actual earnings decreases correctly predicted 
86.69 
94.64 
89.12 
94.58 
AUC (%) 
67.52 
68.62 
67.54 
68.66 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
SAR (%) 
5.02 
9.43 
6.57 
9.74 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
Panel B: Benchmark models 
 
 
 
Compared with 
 
 
 
XBRL/RF 
XBRL/SGB 
 
 
AUC (%) 
 
SAR (%) 
Bootstrap p-value for diff. in: 
Bootstrap p-value for diff. in: 
AUC 
SAR  
AUC 
SAR 
1. OP/Logit 
61.79 
2.48 
<0.01 
<0.01 
<0.01 
<0.01 
    1a. OP/RF 
66.63 
4.67 
0.064 
<0.01 
 
 
    1b. OP/SGB 
66.87 
3.91 
 
 
0.108 
<0.01 
2. DuPont/Logit 
57.96 
1.90 
<0.01 
<0.01 
<0.01 
<0.01 
    2a. DuPont/RF 
61.51 
2.73 
<0.01 
<0.01 
 
 
    2b. DuPont/SGB 
61.15 
2.12 
 
 
<0.01 
<0.01 
3. Analysts’ forecasts 
65.09 
3.38 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
 
 
 
 
 
Panel A presents a summary of prediction performance using XBRL-tagged financial data and different probability cutoffs. Panel B presents a summary of 
prediction performance for different benchmark models and the comparison between them and our machine learning models (XBRL/RF and XBRL/SGB). See 
Sections 4.1 and 4.2 for details. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 61

59 
 
TABLE 6  
Using Detailed Financial Data from Compustat 
 
Panel A: Machine learning models using Compustat data 
 
Random forests 
Stochastic gradient boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
Number of observations 
5,520 
3,338 
5,520 
3,649 
Number of earnings increases 
2,552 
1,362 
2,552 
1,547 
Number of earnings decreases 
2,968 
1,976 
2,968 
2,102 
 
 
 
 
 
% of predicted increases that are actual earnings increases 
60.53 
65.37 
62.37 
65.18 
% correctly predicted 
62.10 
67.51 
63.66 
66.78 
% of actual earnings increases correctly predicted 
35.66 
35.53 
42.67 
43.07 
% of actual earnings decreases correctly predicted 
84.84 
91.57 
81.70 
85.89 
AUC (%) 
67.50 
69.40 
67.39 
68.66 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
SAR (%) 
5.12 
9.74 
3.95 
10.07 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
Panel B: Benchmark models 
 
 
 
Compared with 
 
 
 
COM/RF 
COM/SGB 
 
 
AUC (%) 
 
SAR (%) 
Bootstrap p-value for diff. in: 
Bootstrap p-value for diff. in: 
AUC 
SAR  
AUC 
SAR  
1. OP/Logit 
61.79 
2.48 
<0.01 
<0.01 
<0.01 
<0.01 
    1a. OP/RF 
66.63 
4.67 
0.070 
<0.01 
 
 
    1b. OP/SGB 
66.87 
3.91 
 
 
0.210 
<0.01 
2. DuPont/Logit 
57.96 
1.90 
<0.01 
<0.01 
<0.01 
<0.01 
    2a. DuPont/RF 
61.51 
2.73 
<0.01 
<0.01 
 
 
    2b. DuPont/SGB 
61.15 
2.12 
 
 
<0.01 
<0.01 
3. Analysts’ forecasts 
65.09 
3.38 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
 
 
 
 
 
Panel A presents a summary of prediction performance using all Compustat items and different probability cutoffs. Panel B presents a summary of 
prediction performance for different benchmark models and the comparison between them and our machine learning models (COM/RF and COM/SGB). 
See Section 4.3 for details. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 62

60 
 
TABLE 7 
Additional Analyses for Data Quality 
 
Panel A: Temporal changes in data quality 
 
 
Random forests 
Stochastic gradient 
boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.6 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.6 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
Period 
Early 
Late 
Early 
Late 
AUC (%) 
66.96 
70.88 
68.86 
69.98 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
Bootstrap p-value for diff. in AUC 
0.049 
0.307 
SAR (%) 
2.11 
11.87 
3.98 
11.66 
Bootstrap p-value for SAR vs. 0% 
0.178 
<0.01 
<0.01 
<0.01 
Bootstrap p-value for diff. in SAR  
<0.01 
<0.01 
 
Panel B: Partition on firm-level data quality  
 
 
Random forests 
Stochastic gradient 
boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.6 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.6 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
Data quality 
Low 
High 
Low 
High 
AUC (%) 
65.99 
70.82 
64.70 
71.83 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
Bootstrap p-value for diff. in AUC 
<0.01 
<0.01 
SAR (%) 
8.80 
8.87 
8.67 
9.79 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
Bootstrap p-value for diff. in SAR  
0.014 
<0.01 
 
Panel A shows AUCs and 12-month size-adjusted returns (SAR) of two subsamples by period. The early period is 
2015 and the late period is 2016-2018. Panel B shows AUCs and 12-month size-adjusted returns (SAR) of two 
subsamples based on a firm-level data quality measure. High (low) data quality means the proportion of custom and 
uncommon standard tags in a submission is below (above) the year median value. See Section 4.4 for details. 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 63

61 
 
TABLE 8  
Importance of Predictors 
 
Panel A: Top 10 most important predictors 
Random forests 
Stochastic gradient boosting 
OperatingIncomeLoss t - 1 
NetIncomeLoss t 
ComprehensiveIncomeNetOfTax t - 1 
ComprehensiveIncomeNetOfTax t 
OperatingIncomeLoss t 
NetIncomeLoss t - 1 
EarningsPerShareBasic t - 1 
EarningsPerShareDiluted t - 1 
EarningsPerShareDiluted t 
IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethod
Investments t - 1 
RetainedEarningsAccumulatedDeficit t - 1 
%∆LiabilitiesCurrent 
EarningsPerShareBasicAndDiluted t 
OperatingIncomeLoss t - 1 
EmployeeServiceShareBasedCompensationTaxBenefitFromCompensationExpense t 
IncomeTaxReconciliationChangeInDeferredTaxAssetsValuationAllowance t 
NetIncomeLoss t 
%∆StockholdersEquity 
TreasuryStockValue t - 1 
ShortTermInvestments t - 1 
 
 
Panel B: Top 10 most important predictors in footnotes 
Random forests 
Stochastic gradient boosting 
DeferredTaxAssetsValuationAllowance t 
IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic t 
IncomeTaxReconciliationIncomeTaxExpenseBenefitAtFederalStatutoryIncomeTaxRate t 
IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic t - 1 
DeferredTaxAssetsValuationAllowance t - 1 
IncomeTaxReconciliationIncomeTaxExpenseBenefitAtFederalStatutoryIncomeTaxRate t - 1 
CurrentIncomeTaxExpenseBenefit t 
CurrentFederalTaxExpenseBenefit t 
CurrentFederalTaxExpenseBenefit t - 1 
CurrentIncomeTaxExpenseBenefit t – 1 
 
EmployeeServiceShareBasedCompensationTaxBenefitFromCompensationExpense t 
IncomeTaxReconciliationChangeInDeferredTaxAssetsValuationAllowance t 
%∆AllocatedShareBasedCompensationExpense 
UndistributedEarningsOfForeignSubsidiaries t 
CurrentForeignTaxExpenseBenefit t 
%∆ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber 
%∆DeferredTaxAssetsNetNoncurrent 
%∆CapitalLeasesLesseeBalanceSheetAssetsByMajorClassAccumulatedDeprecation 
%∆ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingWeightedAverage
ExercisePrice 
UnrecognizedTaxBenefitsDecreasesResultingFromSettlementsWithTaxingAuthorities  t 
 
Panel A provides a list of the top 10 most important predictors for random forests and stochastic gradient boosting. Panel B presents a list of the top 10 most 
important predictors in footnotes for random forests and stochastic gradient boosting. The importance of a predictor is computed as the decrease in the 
predictive performance when that variable is randomly shuffled. 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 64

1 
 
 
Online Appendix 
Predicting Future Earnings Changes Using  
Machine Learning and Detailed Financial Data 
 
 
 
This appendix provides supplemental materials that support the manuscript “Predicting 
Future Earnings Changes Using Machine Learning and Detailed Financial Data.”  
 
The materials include the following tables.  
 
Table A1: Predicting direction of earnings changes without requiring pro forma earnings 
Table A2: Excluding the financial industry 
Table A3: Using an alternative definition of uncommon standard tags 
Table A4: Adding an indicator variable for missing values in each prediction 
Table A5: Using the industry-year average to impute missing values 
Table A6: Dropping the %∆ variables 
Table A7: Adding the Fama and French 30 industry indicators 
Table A8: Classification of tags associated with multiple financial statements 
Table A9: Using an alternative drift adjustment 
Table A10: Using the sign of analysts’ forecast errors as a proxy for the direction of earnings 
changes 
Table A11: Using the direction of earnings changes without the drift adjustment 
Table A12: Using market-adjusted returns 
Table A13: Chosen parameter values 
Table A14: Size-adjusted returns by 𝑃𝑃𝑃𝑃
෢ portfolio 
Table A15: Size-adjusted returns by 𝑃𝑃𝑃𝑃
෢ portfolio net of transaction costs 
Table A16: Additional analyses for excess returns 
Table A17: Size-and-book-to-market-adjusted returns 
Table A18: Falsification tests for temporal changes in data quality using detailed financial data 
from Compustat  
Table A19: Predictor importance by financial statement category 
 
Figure A1: Excess returns by year and position 
Figure A2: Excess returns adjusted by size and book-to-market 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 65

2 
 
Table A1: Predicting the direction of earnings changes without requiring pro forma 
earnings 
 
Panel A presents descriptive statistics between our sample and the sample without requiring pro forma earnings. 
ROA is the return on assets, MKVLT is market capitalization, BTM is book-to-market, and LEV is book leverage. 
Panel B presents out-of-sample prediction performance when we use the new sample and US GAAP earnings to 
compute the direction of earnings changes. For each method of random forests and stochastic gradient boosting, two 
sets of probability thresholds are considered. In the first set of probability thresholds, 𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5, we 
assign stocks with a predicted probability of an increase in next year’s earnings greater than (less than or equal to) 
0.5 to the long (short) position. In the second set of probability thresholds, 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4, we assign stocks 
with a predicted probability of an increase in next year’s earnings greater than or equal to 0.6 (less than or equal to 
0.4) to the long (short) position. The resulting 12-month size-adjusted return (SAR) is reported for each hedge 
portfolio. The AUC does not depend on the thresholds, but varies with the sample and the model. The bootstrap p-
value is the proportion of 10,000 bootstrap AUCs that are below 50%. We use a bootstrap sample with the same size 
as the original sample to compute each bootstrap AUC. The bootstrap p-value for SAR is the proportion of 10,000 
pseudo excess returns that are less than 0%. The pseudo excess returns are created under the null hypothesis that the 
XBRL predictors do not have any predictive power. Specifically, for each model, we randomly draw with 
replacement the same number of stocks as those in the long and short positions, compute the 12-month size-adjusted 
returns for this pseudo hedge portfolio, and repeat this process 10,000 times. 
 
Panel A: Descriptive statistics  
 
Our sample (N=8,149) 
 
Without requiring pro forma earnings (N=20,512) 
 
Mean 
Q1 
Median 
Q3 
 
Mean 
Q1 
Median 
Q3 
ROA 
0.013 
0.002 
0.030 
0.065 
 
-0.061 
-0.031 
0.014 
0.056 
MKVLT 
7.692 
6.510 
7.645 
8.870 
 
6.707 
5.255 
6.745 
8.108 
BTM 
0.499 
0.221 
0.398 
0.677 
 
0.576 
0.225 
0.454 
0.781 
LEV 
0.230 
0.043 
0.203 
0.349 
 
0.211 
0.008 
0.139 
0.340 
 
Panel B: Out-of-sample prediction performance 
 
 
Random forests 
Stochastic gradient 
boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
AUC (%) 
60.85 
62.47 
60.69 
61.66 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
Hedge portfolio SAR (%) 
3.32 
4.50 
3.08 
6.39 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 66

3 
 
Table A2: Excluding the financial industry 
This table presents out-of-sample prediction performance when we exclude the financial industry. For each method 
of random forests and stochastic gradient boosting, two sets of probability thresholds are considered. In the first set 
of probability thresholds, 𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5, we assign stocks with a predicted probability of an increase in 
next year’s earnings greater than (less than or equal to) 0.5 to the long (short) position. In the second set of 
probability thresholds, 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4, we assign stocks with a predicted probability of an increase in next 
year’s earnings greater than or equal to 0.6 (less than or equal to 0.4) to the long (short) position. The resulting 12-
month size-adjusted return (SAR) is reported for each hedge portfolio. The AUC does not depend on the thresholds, 
but varies with the sample and the model. The bootstrap p-value is the proportion of 10,000 bootstrap AUCs that are 
below 50%. We use a bootstrap sample with the same size as the original sample to compute each bootstrap AUC. 
The bootstrap p-value for SAR is the proportion of 10,000 pseudo excess returns that are less than 0%. The pseudo 
excess returns are created under the null hypothesis that the XBRL predictors do not have any predictive power. 
Specifically, for each model, we randomly draw with replacement the same number of stocks as those in the long 
and short positions, compute the 12-month size-adjusted returns for this pseudo hedge portfolio, and repeat this 
process 10,000 times. 
 
 
 
Random forests 
Stochastic gradient 
boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
AUC (%) 
68.41 
70.39 
67.71 
69.93 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
Hedge portfolio SAR (%) 
4.50 
10.83 
5.45 
9.28 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 67

4 
 
Table A3: Using an alternative definition of uncommon standard tags 
 
This table presents out-of-sample prediction performance when we exclude all custom tags and uncommon standard 
tags; uncommon standard tags are defined as those that have not been used at least once in each year of the four-year 
rolling window (i.e., two years of a training sample, one year of a validation sample, and one year of a test sample). 
For each method of random forests and stochastic gradient boosting, two sets of probability thresholds are 
considered. In the first set of probability thresholds, 𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5, we assign stocks with a predicted 
probability of an increase in next year’s earnings greater than (less than or equal to) 0.5 to the long (short) position. 
In the second set of probability thresholds, 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4, we assign stocks with a predicted probability of 
an increase in next year’s earnings greater than or equal to 0.6 (less than or equal to 0.4) to the long (short) position. 
The resulting 12-month size-adjusted return (SAR) is reported for each hedge portfolio. The AUC does not depend 
on the thresholds, but varies with the sample and the model. The bootstrap p-value is the proportion of 10,000 
bootstrap AUCs that are below 50%. We use a bootstrap sample with the same size as the original sample to 
compute each bootstrap AUC. The bootstrap p-value for SAR is the proportion of 10,000 pseudo excess returns that 
are less than 0%. The pseudo excess returns are created under the null hypothesis that the XBRL predictors do not 
have any predictive power. Specifically, for each model, we randomly draw with replacement the same number of 
stocks as those in the long and short positions, compute the 12-month size-adjusted returns for this pseudo hedge 
portfolio, and repeat this process 10,000 times. 
 
 
 
Random forests 
Stochastic gradient 
boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
AUC (%) 
67.56 
68.73 
66.97 
67.17 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
Hedge portfolio SAR (%) 
4.20 
11.19 
7.56 
11.35 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 68

5 
 
Table A4: Adding an indicator variable for missing values in each predictor 
 
This table presents out-of-sample prediction performance when we create an indicator variable for missing values in 
each predictor. For each method of random forests and stochastic gradient boosting, two sets of probability 
thresholds are considered. In the first set of probability thresholds, 𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5, we assign stocks with a 
predicted probability of an increase in next year’s earnings greater than (less than or equal to) 0.5 to the long (short) 
position. In the second set of probability thresholds, 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4, we assign stocks with a predicted 
probability of an increase in next year’s earnings greater than or equal to 0.6 (less than or equal to 0.4) to the long 
(short) position. The resulting 12-month size-adjusted return (SAR) is reported for each hedge portfolio. The AUC 
does not depend on the thresholds, but varies with the sample and the model. The bootstrap p-value is the proportion 
of 10,000 bootstrap AUCs that are below 50%. We use a bootstrap sample with the same size as the original sample 
to compute each bootstrap AUC. The bootstrap p-value for SAR is the proportion of 10,000 pseudo excess returns 
that are less than 0%. The pseudo excess returns are created under the null hypothesis that the XBRL predictors do 
not have any predictive power. Specifically, for each model, we randomly draw with replacement the same number 
of stocks as those in the long and short positions, compute the 12-month size-adjusted returns for this pseudo hedge 
portfolio, and repeat this process 10,000 times. 
 
 
 
Random forests 
Stochastic gradient 
boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
AUC (%) 
67.01 
68.35 
67.45 
69.21 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
Hedge portfolio SAR (%) 
2.36 
6.51 
5.11 
9.77 
Bootstrap p-value for SAR vs. 0% 
0.016 
<0.01 
<0.01 
<0.01 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 69

6 
 
Table A5: Using the industry-year average to impute missing values 
 
This table presents out-of-sample prediction performance when we use the industry-year average to impute missing 
values for predictors. For each method of random forests and stochastic gradient boosting, two sets of probability 
thresholds are considered. In the first set of probability thresholds, 𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5, we assign stocks with a 
predicted probability of an increase in next year’s earnings greater than (less than or equal to) 0.5 to the long (short) 
position. In the second set of probability thresholds, 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4, we assign stocks with a predicted 
probability of an increase in next year’s earnings greater than or equal to 0.6 (less than or equal to 0.4) to the long 
(short) position. The resulting 12-month size-adjusted return (SAR) is reported for each hedge portfolio. The AUC 
does not depend on the thresholds, but varies with the sample and the model. The bootstrap p-value is the proportion 
of 10,000 bootstrap AUCs that are below 50%. We use a bootstrap sample with the same size as the original sample 
to compute each bootstrap AUC. The bootstrap p-value for SAR is the proportion of 10,000 pseudo excess returns 
that are less than 0%. The pseudo excess returns are created under the null hypothesis that the XBRL predictors do 
not have any predictive power. Specifically, for each model, we randomly draw with replacement the same number 
of stocks as those in the long and short positions, compute the 12-month size-adjusted returns for this pseudo hedge 
portfolio, and repeat this process 10,000 times. 
 
 
Random forests 
Stochastic gradient boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
AUC (%) 
66.56 
66.37 
67.77 
70.38 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
SAR (%) 
7.31 
7.52 
7.76 
10.41 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 70

7 
 
Table A6: Dropping the %∆ variables 
 
This table presents out-of-sample prediction performance when we drop the 4,627 percentage change predictors (as 
shown in Table 3 Panel A). For each method of random forests and stochastic gradient boosting, two sets of 
probability thresholds are considered. In the first set of probability thresholds, 𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5, we assign 
stocks with a predicted probability of an increase in next year’s earnings greater than (less than or equal to) 0.5 to 
the long (short) position. In the second set of probability thresholds, 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4, we assign stocks with a 
predicted probability of an increase in next year’s earnings greater than or equal to 0.6 (less than or equal to 0.4) to 
the long (short) position. The resulting 12-month size-adjusted return (SAR) is reported for each hedge portfolio. 
The AUC does not depend on the thresholds, but varies with the sample and the model. The bootstrap p-value is the 
proportion of 10,000 bootstrap AUCs that are below 50%. We use a bootstrap sample with the same size as the 
original sample to compute each bootstrap AUC. The bootstrap p-value for SAR is the proportion of 10,000 pseudo 
excess returns that are less than 0%. The pseudo excess returns are created under the null hypothesis that the XBRL 
predictors do not have any predictive power. Specifically, for each model, we randomly draw with replacement the 
same number of stocks as those in the long and short positions, compute the 12-month size-adjusted returns for this 
pseudo hedge portfolio, and repeat this process 10,000 times. 
 
 
 
 
Random forests 
Stochastic gradient 
boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
AUC (%) 
68.00 
69.50 
67.31 
68.72 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
Hedge portfolio SAR (%) 
5.13 
9.29 
6.07 
10.17 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 71

8 
 
Table A7: Adding the Fama and French 30 industry indicators 
 
This table presents out-of-sample prediction performance when we create indicator variables for the Fama-French 
30 industries. For each method of random forests and stochastic gradient boosting, two sets of probability thresholds 
are considered. In the first set of probability thresholds, 𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5, we assign stocks with a predicted 
probability of an increase in next year’s earnings greater than (less than or equal to) 0.5 to the long (short) position. 
In the second set of probability thresholds, 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4, we assign stocks with a predicted probability of 
an increase in next year’s earnings greater than or equal to 0.6 (less than or equal to 0.4) to the long (short) position. 
The resulting 12-month size-adjusted return (SAR) is reported for each hedge portfolio. The AUC does not depend 
on the thresholds, but varies with the sample and the model. The bootstrap p-value is the proportion of 10,000 
bootstrap AUCs that are below 50%. We use a bootstrap sample with the same size as the original sample to 
compute each bootstrap AUC. The bootstrap p-value for SAR is the proportion of 10,000 pseudo excess returns that 
are less than 0%. The pseudo excess returns are created under the null hypothesis that the XBRL predictors do not 
have any predictive power. Specifically, for each model, we randomly draw with replacement the same number of 
stocks as those in the long and short positions, compute the 12-month size-adjusted returns for this pseudo hedge 
portfolio, and repeat this process 10,000 times. 
 
 
 
Random forests 
Stochastic gradient 
boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
AUC (%) 
68.17 
69.01 
67.57 
68.84 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
Hedge portfolio SAR (%) 
4.66 
11.25 
6.23 
9.42 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 72

9 
 
Table A8: Classification of tags associated with multiple financial statements 
 
We classify 4,503 of 4,627 tags into five financial statements and footnote disclosures based on the U.S. GAAP 
taxonomy. The remaining 124 tags are associated with multiple financial statements. This table shows how we 
classify them into the financial statement categories. 
 
Balance Sheet 
Cash 
CashAndCashEquivalentsAtCarryingValue 
RestrictedCashAndCashEquivalents 
RestrictedCashAndCashEquivalentsAtCarryingValue 
DisposalGroupIncludingDiscontinuedOperationCashAndCashEquivalents 
DividendsPayableCurrentAndNoncurrent 
TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests 
RestrictedCashAndCashEquivalentsNoncurrent 
 
Income Statement 
CostOfGoodsSoldDepreciation 
CostOfGoodsSoldAmortization 
CostOfServicesDepreciation 
CostOfServicesAmortization 
InventoryWriteDown 
ProvisionForLoanAndLeaseLosses 
ResearchAndDevelopmentInProcess 
DepreciationNonproduction 
AmortizationOfAcquisitionCosts 
AmortizationOfIntangibleAssets 
AmortizationOfDeferredSalesCommissions 
AmortizationOfRegulatoryAsset 
AmortizationOfLeasedAsset 
AmortizationOfDeferredLeasingFees 
AmortizationOfNuclearFuelLease 
AmortizationOfAdvanceRoyalty 
AmortizationOfDeferredPropertyTaxes 
AmortizationOfDeferredHedgeGains 
OtherAmortizationOfDeferredCharges 
OtherDepreciationAndAmortization 
DepletionOfOilAndGasProperties 
RecapitalizationCosts 
CarryingCostsPropertyAndExplorationRights 
OtherRestructuringCosts 
RestructuringCharges 
EnvironmentalRemediationExpense 
ImpairmentOfLongLivedAssetsToBeDisposedOf 
ImpairmentOfLongLivedAssetsHeldForUse 
ImpairmentOfIntangibleAssetsIndefinitelivedExcludingGoodwill 
GoodwillImpairmentLoss 
ImpairmentOfRealEstate 
ImpairmentOfOngoingProject 
ImpairmentOfLeasehold 
ImpairmentOfIntangibleAssetsFinitelived 
ExplorationAbandonmentAndImpairmentExpense 
ImpairmentOfOilAndGasProperties 
ImpairmentLossesRelatedToRealEstatePartnerships 
DisposalGroupNotDiscontinuedOperationLossGainOnWriteDown 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 73

10 
 
OtherAssetImpairmentCharges 
AssetImpairmentCharges 
AssetRetirementObligationAccretionExpense 
AccretionExpense 
AccretionExpenseIncludingAssetRetirementObligations 
ProductWarrantyExpense 
ProvisionForDoubtfulAccounts 
GainLossOnSaleOfProperty 
GainLossOnDispositionOfAssets 
GainLossOnSaleOfPropertyPlantEquipment 
GainLossOnDispositionOfIntangibleAssets 
EquityMethodInvestmentRealizedGainLossOnDisposal 
GainOrLossOnSaleOfStockInSubsidiary 
GainLossOnSaleOfStockInSubsidiaryOrEquityMethodInvestee 
GainLossOnSaleOfBusiness 
GainLossOnSaleOfOtherAssets 
TradingSecuritiesUnrealizedHoldingGainLoss 
MarketableSecuritiesUnrealizedGainLossExcludingOtherThanTemporaryImpairments 
TradingSecuritiesRealizedGainLoss 
AvailableforsaleSecuritiesGrossRealizedGainLossExcludingOtherThanTemporaryImpairments 
HeldtomaturitySecuritiesSoldSecurityRealizedGainLossExcludingOtherThanTemporaryImpairments 
MarketableSecuritiesRealizedGainLossExcludingOtherThanTemporaryImpairments 
MarketableSecuritiesGainLossExcludingOtherThanTemporaryImpairments 
CostmethodInvestmentsRealizedGainLossExcludingOtherThanTemporaryImpairments 
GainLossOnInvestmentsExcludingOtherThanTemporaryImpairments 
GainLossOnInvestments 
GainLossOnSecuritizationOfFinancialAssets 
DisposalGroupNotDiscontinuedOperationGainLossOnDisposal 
GainLossOnContractTermination 
PublicUtilitiesAllowanceForFundsUsedDuringConstructionAdditions 
ForeignCurrencyTransactionGainLossBeforeTax 
AmortizationOfFinancingCosts 
GainsLossesOnExtinguishmentOfDebt 
IncomeLossFromEquityMethodInvestments 
DiscontinuedOperationIncomeLossFromDiscontinuedOperationBeforeIncomeTax 
DiscontinuedOperationTaxEffectOfDiscontinuedOperation 
IncomeLossFromDiscontinuedOperationsNetOfTax 
ProfitLoss 
NetIncomeLossAttributableToRedeemableNoncontrollingInterest 
NetIncomeLossAttributableToNoncontrollingInterest 
NetIncomeLoss 
IncomeLossIncludingPortionAttributableToNoncontrollingInterest 
AmortizationOfLeaseIncentives 
AmortizationOfMortgageServicingRightsMSRs 
ProvisionForOtherCreditLosses 
ProvisionForOtherLosses 
ProvisionForLoanLeaseAndOtherLosses 
GainLossOnSaleOfEquityInvestments 
GainLossOnSaleOfDebtInvestments 
GainLossOnSaleOfDerivatives 
GainLossOnSaleOfMortgageLoans 
MortgageServicingRightsMSRImpairmentRecovery 
GainLossOnSalesOfLoansNet 
AmortizationOfDeferredLoanOriginationFeesNet 
GainLossOnSaleOfSecuritiesNet 
GainLossOnSaleOfCapitalLeasesNet 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 74

11 
 
GainLossOnSaleOfLeasedAssetsNetOperatingLeases 
GainsLossesOnSalesOfInvestmentRealEstate 
RealizedInvestmentGainsLosses 
DeferredPolicyAcquisitionCostAmortizationExpense 
AmortizationOfValueOfBusinessAcquiredVOBA 
GainLossOnSalesOfAssetsAndAssetImpairmentCharges 
 
Comprehensive Income Statement 
OtherThanTemporaryImpairmentLossesInvestmentsPortionRecognizedInEarningsNet 
OtherComprehensiveIncomeLossNetOfTax 
ComprehensiveIncomeNetOfTaxAttributableToNoncontrollingInterest 
OtherThanTemporaryImpairmentLossesInvestmentsPortionInOtherComprehensiveIncomeLossBeforeTaxPortion
AttributableToParentAvailableforsaleSecurities 
OtherThanTemporaryImpairmentLossesInvestmentsPortionInOtherComprehensiveIncomeLossBeforeTaxIncludi
ngPortionAttributableToNoncontrollingInterestHeldtomaturitySecurities 
OtherThanTemporaryImpairmentLossesInvestmentsPortionInOtherComprehensiveIncomeLossNetOfTaxPortion
AttributableToParentAvailableforsaleSecurities 
OtherThanTemporaryImpairmentLossesInvestmentsPortionInOtherComprehensiveIncomeLossNetOfTaxIncludin
gPortionAttributableToNoncontrollingInterestAvailableforsaleSecurities 
OtherThanTemporaryImpairmentLossesInvestmentsPortionInOtherComprehensiveIncomeLossTaxPortionAttribu
tableToParentAvailableforsaleSecurities 
OtherThanTemporaryImpairmentLossesInvestmentsPortionInOtherComprehensiveIncomeLossTaxIncludingPorti
onAttributableToNoncontrollingInterestHeldtomaturitySecurities 
 
Shareholders’ Equity Statement 
TreasuryStockValue 
StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest 
PreferredStockRedemptionPremium 
PreferredStockRedemptionDiscount 
PreferredStockSharesOutstanding 
CommonStockSharesOutstanding 
CommonStockDividendsPerShareDeclared 
 
 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 75

12 
 
Table A9: Using an alternative drift adjustment 
 
This table presents out-of-sample prediction performance when we use an alternative way to remove the firm-
specific trend by subtracting the lagged change in EPS from the current change in EPS. For each method of random 
forests and stochastic gradient boosting, two sets of probability thresholds are considered. In the first set of 
probability thresholds, 𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5, we assign stocks with a predicted probability of an increase in next 
year’s earnings greater than (less than or equal to) 0.5 to the long (short) position. In the second set of probability 
thresholds, 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4, we assign stocks with a predicted probability of an increase in next year’s 
earnings greater than or equal to 0.6 (less than or equal to 0.4) to the long (short) position. The resulting 12-month 
size-adjusted return (SAR) is reported for each hedge portfolio. The AUC does not depend on the thresholds, but 
varies with the sample and the model. The bootstrap p-value is the proportion of 10,000 bootstrap AUCs that are 
below 50%. We use a bootstrap sample with the same size as the original sample to compute each bootstrap AUC. 
The bootstrap p-value for SAR is the proportion of 10,000 pseudo excess returns that are less than 0%. The pseudo 
excess returns are created under the null hypothesis that the XBRL predictors do not have any predictive power. 
Specifically, for each model, we randomly draw with replacement the same number of stocks as those in the long 
and short positions, compute the 12-month size-adjusted returns for this pseudo hedge portfolio, and repeat this 
process 10,000 times. 
 
 
Random forests 
Stochastic gradient boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
Number of observations 
5,520 
3,342 
5,520 
3,883 
Number of earnings increases 
2,634 
1,370 
2,634 
1,660 
Number of earnings decreases 
2,886 
1,972 
2,886 
2,223 
 
 
 
 
 
AUC (%) 
67.53 
68.37 
67.49 
68.55 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
SAR (%) 
4.63 
8.79 
5.57 
8.99 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 76

13 
 
Table A10: Using the sign of analysts’ forecast errors as a proxy for the direction of 
earnings changes 
 
This table presents out-of-sample prediction performance when we use the sign of analysts’ forecast errors as a 
proxy for the direction of earnings changes. Specifically, we compare actual earnings in fiscal year t + 1 with the 
consensus analyst forecast issued in the month following the earnings release for fiscal year t to define an earnings 
increase/decrease. For each method of random forests and stochastic gradient boosting, two sets of probability 
thresholds are considered. In the first set of probability thresholds, 𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5, we assign stocks with a 
predicted probability of an increase in next year’s earnings greater than (less than or equal to) 0.5 to the long (short) 
position. In the second set of probability thresholds, 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4, we assign stocks with a predicted 
probability of an increase in next year’s earnings greater than or equal to 0.6 (less than or equal to 0.4) to the long 
(short) position. The resulting 12-month size-adjusted return (SAR) is reported for each hedge portfolio. The AUC 
does not depend on the thresholds, but varies with the sample and the model. The bootstrap p-value is the proportion 
of 10,000 bootstrap AUCs that are below 50%. We use a bootstrap sample with the same size as the original sample 
to compute each bootstrap AUC. The bootstrap p-value for SAR is the proportion of 10,000 pseudo excess returns 
that are less than 0%. The pseudo excess returns are created under the null hypothesis that the XBRL predictors do 
not have any predictive power. Specifically, for each model, we randomly draw with replacement the same number 
of stocks as those in the long and short positions, compute the 12-month size-adjusted returns for this pseudo hedge 
portfolio, and repeat this process 10,000 times. 
 
 
 
Random forests 
Stochastic gradient 
boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
AUC (%) 
57.38 
58.64 
56.57 
58.13 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
Hedge portfolio SAR (%) 
2.52 
11.20 
4.24 
8.81 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 77

14 
 
Table A11: Using the direction of earnings changes without the drift adjustment 
 
This table presents out-of-sample prediction performance when we do not remove the firm-specific trend from the 
current change in EPS. For each method of random forests and stochastic gradient boosting, two sets of probability 
thresholds are considered. In the first set of probability thresholds, 𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5, we assign stocks with a 
predicted probability of an increase in next year’s earnings greater than (less than or equal to) 0.5 to the long (short) 
position. In the second set of probability thresholds, 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4, we assign stocks with a predicted 
probability of an increase in next year’s earnings greater than or equal to 0.6 (less than or equal to 0.4) to the long 
(short) position. The resulting 12-month size-adjusted return (SAR) is reported for each hedge portfolio. The AUC 
does not depend on the thresholds, but varies with the sample and the model. The bootstrap p-value is the proportion 
of 10,000 bootstrap AUCs that are below 50%. We use a bootstrap sample with the same size as the original sample 
to compute each bootstrap AUC. The bootstrap p-value for SAR is the proportion of 10,000 pseudo excess returns 
that are less than 0%. The pseudo excess returns are created under the null hypothesis that the XBRL predictors do 
not have any predictive power. Specifically, for each model, we randomly draw with replacement the same number 
of stocks as those in the long and short positions, compute the 12-month size-adjusted returns for this pseudo hedge 
portfolio, and repeat this process 10,000 times. 
 
 
Random forests 
Stochastic gradient boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
Number of observations 
5,520 
3,342 
5,520 
3,883 
Number of earnings increases 
3,698 
2,442 
3,698 
2,750 
Number of earnings decreases 
1,822 
951 
1,822 
1,137 
 
 
 
 
 
AUC (%) 
67.83 
68.79 
66.57 
67.23 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
SAR (%) 
4.79 
10.08 
6.77 
10.55 
Bootstrap p-value for SAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 78

15 
 
Table A12: Using market-adjusted returns 
 
This table presents out-of-sample prediction performance when we use the market-adjusted returns. For each method 
of random forests and stochastic gradient boosting, two sets of probability thresholds are considered. In the first set 
of probability thresholds, 𝑃𝑃𝑃𝑃
෢> 0.5 and 𝑃𝑃𝑃𝑃
෢≤ 0.5, we assign stocks with a predicted probability of an increase in 
next year’s earnings greater than (less than or equal to) 0.5 to the long (short) position. In the second set of 
probability thresholds, 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 𝑃𝑃𝑃𝑃
෢≤ 0.4, we assign stocks with a predicted probability of an increase in next 
year’s earnings greater than or equal to 0.6 (less than or equal to 0.4) to the long (short) position. The resulting 12-
month market-adjusted return (MAR) is reported for each hedge portfolio. The AUC does not depend on the 
thresholds, but varies with the sample and the model. The bootstrap p-value is the proportion of 10,000 bootstrap 
AUCs that are below 50%. We use a bootstrap sample with the same size as the original sample to compute each 
bootstrap AUC. The bootstrap p-value for MAR is the proportion of 10,000 pseudo excess returns that are less than 
0%. The pseudo excess returns are created under the null hypothesis that the XBRL predictors do not have any 
predictive power. Specifically, for each model, we randomly draw with replacement the same number of stocks as 
those in the long and short positions, compute the 12-month market-adjusted returns for this pseudo hedge portfolio, 
and repeat this process 10,000 times. 
 
 
 
Random forests 
Stochastic gradient 
boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
AUC (%) 
67.52 
68.62 
67.54 
68.66 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
Hedge portfolio MAR (%) 
5.97 
12.08 
7.37 
11.15 
Bootstrap p-value for MAR vs. 0% 
<0.01 
<0.01 
<0.01 
<0.01 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 79

16 
 
Table A13: Chosen parameter values 
This table shows the chosen parameter values for the two machine learning methods for each test year. 
Random forests 
 
2015 
2016 
2017 
2018 
# of variables (k) 
114 
116 
115 
120 
# of trees (m) 
1,000 
1,000 
1,000 
1,000 
Min. # of obs. in a leaf (b) 
1 
3 
2 
3 
 
 
 
 
 
Stochastic gradient boosting 
 
2015 
2016 
2017 
2018 
# of trees (m) 
800 
700 
500 
700 
Learning rate (ρ) 
0.005 
0.01 
0.01 
0.005 
Tree depth (L) 
4 
3 
3 
4 
 
 
 
 
 
 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 80

17 
 
Table A14: Size-adjusted returns by 𝑷𝑷𝑷𝑷
෢ portfolio 
 
This table presents 12-month size-adjusted returns on portfolios based on 𝑃𝑃𝑃𝑃
෢, the estimated probability of an 
increase in next year’s earnings in the test period (2015-2018). For each portfolio, the number of observations (N), 
the proportion of accurate predictions (% correctly predicted), and 12-month size-adjusted returns (SAR) are 
reported. % correctly predicted is the proportion of observations with a correct prediction using 𝑃𝑃𝑃𝑃
෢= 0.5 as a cutoff. 
We consider two hedge portfolios. The first hedge portfolio takes a long (short) position in stocks with 𝑃𝑃𝑃𝑃
෢> 0.5 (≤ 
0.5). The second hedge portfolio takes a long (short) position in stocks with 𝑃𝑃𝑃𝑃
෢≥ 0.6 (≤ 0.4). The resulting 12-
month size-adjusted return is reported for each hedge portfolio. The p-values in parentheses pertain to 12-month 
size-adjusted abnormal returns and are calculated from a bootstrap distribution of 10,000 pseudo abnormal returns 
under the null hypothesis that our predictors do not have any predictive power. For each of 10,000 iterations, we 
randomly assign stocks to the long and short positions and calculate pseudo 12-month size-adjusted returns. The 12-
month size-adjusted returns for the perfect foresight strategy are calculated from taking a long (short) position in 
stocks with an earnings increase (decrease) in the next year. ***, **, and * denote statistical significance at the 0.01, 
0.05, and 0.1 levels, respectively. 
Random forests 
𝑃𝑃𝑟𝑟
෢ portfolio 
𝑃𝑃𝑃𝑃
෢ values 
N 
% correctly predicted 
12-month SAR (%) 
 
1 
𝑃𝑃𝑃𝑃
෢≤0.1 
31 
83.87 
-7.71 
 
2 
0.1 < 𝑃𝑃𝑃𝑃
෢≤0.2  
336 
79.17 
-0.53 
 
3 
0.2 < 𝑃𝑃𝑃𝑃
෢≤0.3  
1,009 
69.87 
1.88 
 
4 
0.3 < 𝑃𝑃𝑃𝑃
෢≤0.4 
1,473 
59.27 
1.71 
 
5 
0.4 < 𝑃𝑃𝑃𝑃
෢≤0.5 
1,432 
49.09 
1.70 
 
6 
0.5 < 𝑃𝑃𝑃𝑃
෢≤0.6 
754 
61.67 
3.72 
 
7 
0.6 < 𝑃𝑃𝑃𝑃
෢≤0.7 
266 
71.05 
5.68 
 
8 
0.7 < 𝑃𝑃𝑃𝑃
෢≤0.8 
150 
84.00 
15.36 
 
9 
𝑃𝑃𝑃𝑃
෢> 0.8 
69 
92.75 
23.90 
 
 
 
 
 
 
 
Hedge portfolio 
 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
 
 
5.02*** 
(<0.01) 
 
Short 
𝑃𝑃𝑃𝑃
෢≤0.5 
 
 
Perfect foresight 
 
 
 
12.97 
 
 
 
 
 
 
 
Hedge portfolio 
 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢≥0.6 
 
 
9.43*** 
(<0.01) 
 
Short 
𝑃𝑃𝑃𝑃
෢≤0.4 
 
 
Perfect foresight 
 
 
 
12.85 
 
Stochastic gradient boosting 
𝑃𝑃𝑃𝑃
෢ portfolio 
𝑃𝑃𝑃𝑃
෢ values 
N 
% correctly predicted 
12-month SAR (%) 
 
1 
𝑃𝑃𝑃𝑃
෢≤0.2  
33 
87.88 
6.26 
 
2 
0.2 < 𝑃𝑃𝑃𝑃
෢≤0.3  
1,141 
73.09 
0.59 
 
3 
0.3 < 𝑃𝑃𝑃𝑃
෢≤0.4 
1,908 
58.96 
1.61 
 
4 
0.4 < 𝑃𝑃𝑃𝑃
෢≤0.5 
1,323 
49.66 
1.45 
 
5 
0.5 < 𝑃𝑃𝑃𝑃
෢≤0.6 
548 
61.86 
4.34 
 
6 
0.6 < 𝑃𝑃𝑃𝑃
෢≤0.7 
284 
73.94 
9.45 
 
7 
0.7 < 𝑃𝑃𝑃𝑃
෢≤0.8 
209 
84.21 
14.27 
 
8 
𝑃𝑃𝑃𝑃
෢> 0.8 
74 
90.54 
10.36 
 
 
 
 
 
 
 
Hedge portfolio 
 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
 
 
6.57*** 
(<0.01) 
 
Short 
𝑃𝑃𝑃𝑃
෢≤0.5 
 
 
Perfect foresight 
 
 
 
12.97 
 
 
 
 
 
 
 
Hedge portfolio 
 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢≥0.6 
 
 
9.74*** 
(<0.01) 
 
Short 
𝑃𝑃𝑃𝑃
෢≤0.4 
 
 
Perfect foresight 
 
 
 
13.03 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 81

18 
 
Table A15: Size-adjusted returns by 𝑷𝑷𝑷𝑷
෢ portfolio net of transaction costs 
 
This table presents 12-month size-adjusted returns net of transaction costs on portfolios based on 𝑃𝑃𝑃𝑃
෢, the estimated 
probability of an increase in next year’s earnings in the test period (2015-2018). The transaction costs are estimated 
as the effective bid-ask spread following Novy-Marx and Velikov [2016]. For each portfolio, the number of 
observations (N), the proportion of accurate predictions (% correctly predicted), and 12-month size-adjusted returns 
(SAR) are reported. % correctly predicted is the proportion of observations with a correct prediction using 𝑃𝑃𝑃𝑃
෢= 0.5 
as a cutoff. We consider two hedge portfolios. The first hedge portfolio takes a long (short) position in stocks with 
𝑃𝑃𝑃𝑃
෢> 0.5 (≤ 0.5). The second hedge portfolio takes a long (short) position in stocks with 𝑃𝑃𝑃𝑃
෢≥ 0.6 (≤ 0.4). The 
resulting 12-month size-adjusted return is reported for each hedge portfolio. The p-values in parentheses pertain to 
12-month size-adjusted abnormal returns and are calculated from a bootstrap distribution of 10,000 pseudo abnormal 
returns under the null hypothesis that our predictors do not have any predictive power. For each of 10,000 iterations, 
we randomly assign stocks to the long and short positions and calculate pseudo 12-month size-adjusted returns. The 
12-month size-adjusted returns for the perfect foresight strategy are calculated from taking a long (short) position in 
stocks with an earnings increase (decrease) in the next year. ***, **, and * denote statistical significance at the 0.01, 
0.05, and 0.1 levels, respectively. 
Random forest 
𝑃𝑃𝑃𝑃
෢ portfolio 
𝑃𝑃𝑃𝑃
෢ values 
N 
% correctly predicted 
12-month SAR (%) 
 
1 
𝑃𝑃𝑃𝑃
෢≤0.1 
31 
83.87 
-8.08 
 
2 
0.1 < 𝑃𝑃𝑃𝑃
෢≤0.2  
336 
79.17 
-1.01 
 
3 
0.2 < 𝑃𝑃𝑃𝑃
෢≤0.3  
1,009 
69.87 
1.41 
4 
0.3 < 𝑃𝑃𝑃𝑃
෢≤0.4 
1,473 
59.27 
1.23 
 
5 
0.4 < 𝑃𝑃𝑃𝑃
෢≤0.5 
1,432 
49.09 
1.15 
 
6 
0.5 < 𝑃𝑃𝑃𝑃
෢≤0.6 
754 
61.67 
3.08 
 
7 
0.6 < 𝑃𝑃𝑃𝑃
෢≤0.7 
266 
71.05 
4.82 
 
8 
0.7 < 𝑃𝑃𝑃𝑃
෢≤0.8 
150 
84.00 
14.32 
9 
𝑃𝑃𝑃𝑃
෢> 0.8 
69 
92.75 
23.02 
 
 
 
 
 
 
 
Hedge portfolio 
 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
 
 
4.77*** 
(<0.01) 
 
Short 
𝑃𝑃𝑃𝑃
෢≤0.5 
 
 
Perfect foresight 
 
 
 
12.95 
 
 
 
 
 
 
 
Hedge portfolio 
 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢≥0.6 
 
 
8.98*** 
(<0.01) 
 
Short 
𝑃𝑃𝑃𝑃
෢≤0.4 
 
 
Perfect foresight 
 
 
 
12.80 
 
Stochastic gradient boosting 
𝑃𝑃𝑃𝑃
෢ portfolio 
𝑃𝑃𝑃𝑃
෢ values 
N 
% correctly predicted 
12-month SAR (%) 
 
1 
𝑃𝑃𝑃𝑃
෢≤0.2  
33 
87.88 
5.68 
 
2 
0.2 < 𝑃𝑃𝑃𝑃
෢≤0.3  
1,141 
73.09 
0.10 
 
3 
0.3 < 𝑃𝑃𝑃𝑃
෢≤0.4 
1,908 
58.96 
1.13 
 
4 
0.4 < 𝑃𝑃𝑃𝑃
෢≤0.5 
1,323 
49.66 
0.94 
 
5 
0.5 < 𝑃𝑃𝑃𝑃
෢≤0.6 
548 
61.86 
3.65 
 
6 
0.6 < 𝑃𝑃𝑃𝑃
෢≤0.7 
284 
73.94 
8.59 
 
7 
0.7 < 𝑃𝑃𝑃𝑃
෢≤0.8 
209 
84.21 
13.28 
 
8 
𝑃𝑃𝑃𝑃
෢> 0.8 
74 
90.54 
9.38 
 
 
 
 
 
 
 
Hedge portfolio 
 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
 
 
6.25*** 
(<0.01) 
 
Short 
𝑃𝑃𝑃𝑃
෢≤0.5 
 
 
Perfect foresight 
 
 
 
12.95 
 
 
 
 
 
 
 
Hedge portfolio 
 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢≥0.6 
 
 
9.30*** 
(<0.01) 
 
Short 
𝑃𝑃𝑃𝑃
෢≤0.4 
 
 
Perfect foresight 
 
 
 
12.98 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 82

19 
 
Table A16: Additional analyses for excess returns 
 
Panel A presents 12-month size-adjusted returns (SAR) net of transaction costs on portfolios based on 𝑃𝑃𝑃𝑃
෢, the 
estimated probability of an increase in next year’s earnings using two alternative earnings measures (ROEt + 1 and 
EBITt + 1) in the test period (2015-2018). ROE is defined as net income divided by book value of equity at the fiscal 
year-end. EBIT is defined as earnings before interest and tax deflated by the number of common shares outstanding 
at the fiscal year-end. The transaction costs are estimated as the effective bid-ask spread following Novy-Marx and 
Velikov [2016]. Panel B presents 12-month size-adjusted returns (SAR) net of transaction costs on portfolios based 
on 𝑃𝑃𝑃𝑃
෢, the estimated probability of an increase in next year’s earnings, excluding microcaps. Microcaps are defined 
as those with market capitalization of less than the 20th percentile of NYSE market capitalization. For each method 
of random forests and stochastic gradient boosting, long (short) positions are taken in stocks with a predicted 
probability of an increase in next year’s earnings greater than or equal to 0.6 (less than or equal to 0.4). The p-values 
in parentheses are calculated from a bootstrap distribution of 10,000 pseudo abnormal returns under the null 
hypothesis that our predictors do not have any predictive power. For each of 10,000 iterations, we randomly assign 
stocks to the long and short positions and calculate pseudo 12-month size-adjusted returns net of transaction costs. 
The 12-month size-adjusted returns net of transaction costs for the perfect foresight strategy are calculated from 
taking long (short) positions in stocks with an increase (a decrease) in next year’s earnings. Panel C presents results 
from regressing excess returns of our trading strategies on Fama-French’s [2015] five-factors. For each method of 
random forests and stochastic gradient boosting, long (short) positions are taken in stocks with a predicted 
probability of an increase in next year’s earnings greater than or equal to 0.6 (less than or equal to 0.4). In columns 
(1) and (3), the dependent variable is the monthly hedge portfolio returns in excess of 1-month T-bill rate. In 
columns (2) and (4), we adjust the dependent variable by subtracting Fama-French 30 industry monthly returns for 
each stock in the portfolio. The explanatory variables are the market returns in excess of 1-month T-bill rate (𝑅𝑅𝑀𝑀𝑀𝑀−
𝑅𝑅𝐹𝐹𝐹𝐹), and returns on the size (SMB), book-to-market (HML), profitability (RMW), and investment (CMA) portfolios. 
Returns are in percentage. Newey-West standard errors are presented in parentheses.  ***, **, and * denote 
statistical significance at the 0.01, 0.05, and 0.10 levels, respectively. 
 
Panel A: Alternative earnings measures 
 
ROE t + 1 
EBIT t + 1 
 
RF 
SGB 
RF 
SGB 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
SAR net of transaction costs (%) 
6.46*** 
5.43*** 
8.92*** 
8.52*** 
p-value 
(<0.01) 
(<0.01) 
(<0.01) 
(<0.01) 
Perfect foresight SAR net of transaction costs (%) 
9.98 
10.68 
14.32 
13.35 
 
 
 
 
 
 
Panel B: Excluding microcaps 
 
Random forests 
Stochastic gradient boosting 
Probability thresholds 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
SAR net of transaction costs (%) 
7.65*** 
7.34*** 
p-value 
(<0.01) 
(<0.01) 
Perfect foresight SAR net of transaction costs (%) 
10.69 
10.74 
 
 
 
 
 
 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 83

20 
 
Panel C: Controlling for five-factors 
Dep. Var. = 
𝑅𝑅𝑝𝑝𝑝𝑝−𝑅𝑅𝐹𝐹𝐹𝐹 
 
Random forests 
Stochastic gradient boosting 
 
(1) 
(2) 
(3) 
(4) 
Intercept 
0.79** 
0.66* 
0.94*** 
0.69** 
 
(0.34) 
(0.33) 
(0.27) 
(0.31) 
𝑅𝑅𝑀𝑀𝑀𝑀−𝑅𝑅𝐹𝐹𝐹𝐹 
0.01 
-0.06 
0.05 
-0.04 
 
(0.11) 
(0.14) 
(0.13) 
(0.18) 
𝑆𝑆𝑆𝑆𝐵𝐵𝑡𝑡 
0.33** 
0.52*** 
0.53*** 
0.67*** 
 
(0.14) 
(0.16) 
(0.13) 
(0.17) 
𝐻𝐻𝐻𝐻𝐿𝐿𝑡𝑡 
-0.54** 
-0.30 
-0.56** 
-0.31 
 
(0.25) 
(0.24) 
(0.22) 
(0.21) 
𝑅𝑅𝑅𝑅𝑊𝑊𝑡𝑡 
-0.26 
-0.10 
-0.34 
-0.22 
 
(0.23) 
(0.28) 
(0.21) 
(0.29) 
𝐶𝐶𝐶𝐶𝐴𝐴𝑡𝑡 
-0.32 
-0.03 
0.09 
0.23 
 
(0.28) 
(0.26) 
(0.25) 
(0.24) 
Industry-adjusted 
No 
Yes 
No 
Yes 
N 
51 
51 
51 
51 
R2 
0.32 
0.19 
0.37 
0.26 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 84

21 
 
Table A17: Size-and-book-to-market-adjusted returns 
 
This table presents excess returns adjusted by size and book-to-market. The size-and-book-to-market-adjusted 
returns are calculated by subtracting the value-weighted buy-and-hold returns on the size-and-book-to-market-
matched 5x5 portfolio from the buy-and-hold sample firm returns. When constructing the size-and-book-to-market 
portfolios, all the NYSE firms are ranked according to their market value of equity to be placed into quintiles and 
also according to their book-to-market to be placed into quintiles. Then, NASDAQ and AMEX firms are put into 
one of the 25 intersecting portfolios based on their market value of equity and book-to-market. The annual return for 
each of the 25 intersecting portfolios is calculated by taking the market value-weighted average of the annual returns 
of the constituent firms. 
 
 
Size-and-book-to-market-adjusted 
 
Random forests 
Stochastic gradient boosting 
Probability thresholds 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.5 
𝑃𝑃𝑃𝑃
෢≥ 0.5 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
𝑃𝑃𝑃𝑃
෢≤ 0.5 
 
 
 
Hedge portfolio excess returns (%) 
5.02 
6.56 
Bootstrap p-value for excess returns vs. 0% 
<0.01 
<0.01 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 85

22 
 
Table A18: Falsification tests for temporal changes in data quality using detailed financial 
data from Compustat  
 
This table shows the AUCs of two subsamples by period using detailed financial data from Compustat, which do not 
experience the same data quality changes as XBRL documents. The early period is 2015 and the late period is 2016-
2018. The bootstrap p-value for AUC difference is the proportion of 10,000 bootstrap AUC differences that are 
below zero. We use a bootstrap sample with the same size as the original subsample to compute the bootstrap AUC 
for each subsample and the AUC difference between the two subsamples. 
 
 
 
Random forests 
Stochastic gradient 
boosting 
Probability thresholds 
 
 
 
 
 
Long 
𝑃𝑃𝑃𝑃
෢> 0.6 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
𝑃𝑃𝑃𝑃
෢> 0.6 
𝑃𝑃𝑃𝑃
෢≥ 0.6 
 
Short 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
𝑃𝑃𝑃𝑃
෢≤ 0.4 
 
 
 
 
 
Period 
Early 
Late 
Early 
Late 
AUC (%) 
69.56 
69.68 
67.49 
67.93 
Bootstrap p-value for AUC vs. 50% 
<0.01 
<0.01 
<0.01 
<0.01 
Bootstrap p-value for diff. in AUC 
0.4774 
0.4184 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 86

23 
 
Table A19: Predictor importance by financial statement category 
 
Panel A: Top 10 most important predictors by financial statement category (random forests) 
Balance Sheet 
Cash Flow Statement 
RetainedEarningsAccumulatedDeficit t 
RetainedEarningsAccumulatedDeficit t - 1 
%∆LiabilitiesCurrent 
%∆StockholdersEquity 
OtherAssetsNoncurrent t - 1 
LiabilitiesCurrent t 
AssetsCurrent t 
OtherAssetsNoncurrent t 
%∆EmployeeRelatedLiabilitiesCurrent 
AccumulatedOtherComprehensiveIncomeLossNetOfTax t 
NetCashProvidedByUsedInOperatingActivities t - 1 
NetCashProvidedByUsedInOperatingActivities t 
ShareBasedCompensation t 
IncomeTaxesPaid t 
%∆DeferredIncomeTaxExpenseBenefit 
NetCashProvidedByUsedInOperatingActivitiesContinuingOperations t 
IncreaseDecreaseInAccountsReceivable t - 1 
NetCashProvidedByUsedInInvestingActivities t - 1 
CashAndCashEquivalentsPeriodIncreaseDecrease t 
%∆CashAndCashEquivalentsPeriodIncreaseDecrease 
Income Statement 
Comprehensive Income Statement 
OperatingIncomeLoss t - 1 
NetIncomeLoss t 
OperatingIncomeLoss t 
NetIncomeLoss t - 1 
EarningsPerShareBasic t - 1 
EarningsPerShareDiluted t - 1 
EarningsPerShareDiluted t 
IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethod
Investments t - 1 
EarningsPerShareBasic t 
EarningsPerShareBasicAndDiluted t 
ComprehensiveIncomeNetOfTax t - 1 
ComprehensiveIncomeNetOfTax t 
%∆ComprehensiveIncomeNetOfTax 
OtherComprehensiveIncomeLossNetOfTax  t - 1 
ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest t - 1 
OtherComprehensiveIncomeLossNetOfTax  t 
%∆OtherComprehensiveIncomeLossNetOfTax 
%∆OtherComprehensiveIncomeLossForeignCurrencyTransactionAndTranslationAdjustmentNetOfTax 
OtherComprehensiveIncomeLossForeignCurrencyTransactionAndTranslationAdjustmentNetOfTax t 
ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest t 
 
Shareholders’ Equity Statement 
Footnotes 
AdjustmentsToAdditionalPaidInCapitalSharebasedCompensationRequisiteServicePeriodRecognitionValue t 
CommonStockSharesOutstanding t 
TreasuryStockValue t - 1 
%∆CommonStockSharesOutstanding 
TreasuryStockValue t 
CommonStockSharesOutstanding t - 1 
AdjustmentsToAdditionalPaidInCapitalSharebasedCompensationRequisiteServicePeriodRecognitionValue t - 1 
%∆AdjustmentsToAdditionalPaidInCapitalSharebasedCompensationRequisiteServicePeriodRecognitionValue 
%∆TreasuryStockValue 
%∆StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest 
DeferredTaxAssetsValuationAllowance t 
IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic t 
IncomeTaxReconciliationIncomeTaxExpenseBenefitAtFederalStatutoryIncomeTaxRate t 
IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic t - 1 
DeferredTaxAssetsValuationAllowance t - 1 
IncomeTaxReconciliationIncomeTaxExpenseBenefitAtFederalStatutoryIncomeTaxRate t - 1 
CurrentIncomeTaxExpenseBenefit t 
CurrentFederalTaxExpenseBenefit t 
CurrentFederalTaxExpenseBenefit t - 1 
CurrentIncomeTaxExpenseBenefit t - 1 
 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 87

24 
 
Panel B: Top 10 most important predictors by financial statement category (stochastic gradient boosting) 
Balance Sheet 
Cash Flow Statement 
RetainedEarningsAccumulatedDeficit t - 1 
%∆LiabilitiesCurrent 
%∆StockholdersEquity 
ShortTermInvestments t - 1 
%∆EmployeeRelatedLiabilitiesCurrent 
AccountsPayableCurrent t - 1 
AccruedIncomeTaxesCurrent t - 1 
%∆AccruedIncomeTaxesCurrent 
LongTermDebtAndCapitalLeaseObligations t - 1 
TradingSecurities t - 1 
PaymentsForRepurchaseOfCommonStock t - 1 
%∆ShareBasedCompensation 
PaymentsForRepurchaseOfCommonStock t 
EffectOfExchangeRateOnCashAndCashEquivalents t 
IncomeTaxesPaid t 
ProceedsFromSaleOfAvailableForSaleSecurities t - 1 
NetCashProvidedByUsedInInvestingActivities t - 1 
%∆IncomeTaxesPaid 
NetCashProvidedByUsedInInvestingActivities t 
ProceedsFromInsuranceSettlementInvestingActivities t - 1 
 
 
Income Statement 
Comprehensive Income Statement 
EarningsPerShareBasicAndDiluted t 
OperatingIncomeLoss t - 1 
NetIncomeLoss t 
%∆IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMet
hodInvestments 
%∆NetIncomeLoss 
BusinessCombinationAcquisitionRelatedCosts t - 1 
EarningsPerShareBasic t - 1 
ProfitLoss t 
AntidilutiveSecuritiesExcludedFromComputationOfEarningsPerShareAmount t 
NetIncomeLossAvailableToCommonStockholdersBasic t 
ComprehensiveIncomeNetOfTax t - 1 
OtherComprehensiveIncomeLossForeignCurrencyTransactionAndTranslationAdjustmentNetOfTax t 
%∆OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent 
OtherComprehensiveIncomeLossNetOfTax  t 
OtherComprehensiveIncomeLossNetOfTaxPortionAttributableToParent t 
%∆OtherComprehensiveIncomeUnrealizedHoldingGainLossOnSecuritiesArisingDuringPeriodTax 
OtherComprehensiveIncomeUnrealizedHoldingGainLossOnSecuritiesArisingDuringPeriodTax t - 1 
OtherComprehensiveIncomeLossDerivativesQualifyingAsHedgesNetOfTax t - 1 
OtherComprehensiveIncomeUnrealizedHoldingGainLossOnSecuritiesArisingDuringPeriodNetOfTax t - 1 
OtherComprehensiveIncomeLossPensionAndOtherPostretirementBenefitPlansNetUnamortizedGainLossArisin
gDuringPeriodBeforeTax t - 1 
 
 
Shareholders’ Equity Statement 
Footnotes 
TreasuryStockValue t - 1 
StockIssuedDuringPeriodValueNewIssues t 
StockIssuedDuringPeriodValueNewIssues t - 1 
StockholdersEquityOther t - 1 
StockIssuedDuringPeriodValueStockOptionsExercised t 
%∆StockIssuedDuringPeriodValueNewIssues 
%∆DividendsCommonStockCash 
TreasuryStockValue t 
%∆CommonStockDividendsPerShareDeclared 
%∆TreasuryStockValue 
 
EmployeeServiceShareBasedCompensationTaxBenefitFromCompensationExpense t 
IncomeTaxReconciliationChangeInDeferredTaxAssetsValuationAllowance t 
%∆AllocatedShareBasedCompensationExpense 
UndistributedEarningsOfForeignSubsidiaries t 
CurrentForeignTaxExpenseBenefit t 
%∆ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber 
%∆DeferredTaxAssetsNetNoncurrent 
%∆CapitalLeasesLesseeBalanceSheetAssetsByMajorClassAccumulatedDeprecation 
%∆ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingWeightedAverage
ExercisePrice 
UnrecognizedTaxBenefitsDecreasesResultingFromSettlementsWithTaxingAuthorities  t 
 
 
 
Panel A presents a list of the top 10 most important predictors by financial statement category for random forests. Panel B presents a list of the top 10 most 
important predictors by financial statement category for stochastic gradient boosting. Each predictor is classified into balance sheet, cash flow statement, 
income statement, comprehensive income statement, shareholders’ equity statement, or footnotes. The importance of a predictor is computed as the decrease in 
the predictive performance when that variable is randomly shuffled. 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 88

25 
 
Figure A1: Excess returns by year and position 
Figure A1a presents 12-month size-adjusted returns from taking long positions in stocks with 𝑃𝑃𝑃𝑃
෢> 0.5 and short positions in stocks with 𝑃𝑃𝑃𝑃
෢≤ 0.5 using random 
forests. Figure A1b presents 12-month size-adjusted returns from taking long positions in stocks with 𝑃𝑃𝑟𝑟
෢≥ 0.6 and short positions in stocks with 𝑃𝑃𝑃𝑃
෢≤ 0.4 using 
random forests. Figure A1c shows 12-month size-adjusted returns from taking long positions in stocks with 𝑃𝑃𝑃𝑃
෢> 0.5 and short positions in stocks with 𝑃𝑃𝑃𝑃
෢≤ 0.5 
using stochastic gradient boosting. Figure A1d shows 12-month size-adjusted returns from taking long positions in stocks with 𝑃𝑃𝑃𝑃
෢≥ 0.6 and short positions in 
stocks with 𝑃𝑃𝑃𝑃
෢≤ 0.4 using stochastic gradient boosting. The size-adjusted return for a hedge portfolio is the difference between the size-adjusted abnormal 
returns of the long and short positions. 
 
 
 
Figure A1a Random forests with 𝑃𝑃𝑃𝑃
෢ > 0.5 and <0.5 
 
Figure A1b Random forests with 𝑃𝑃𝑃𝑃
෢ > 0.6 and <0.4 
 
 
 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 89

26 
 
 
 
Figure A1c Stochastic gradient boosting with 𝑃𝑃𝑃𝑃
෢ > 0.5 and <0.5 
 
Figure A1d Stochastic gradient boosting with 𝑃𝑃𝑃𝑃
෢ > 0.6 and <0.4 
 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 90

27 
 
Figure A2: Excess returns adjusted by size and book-to-market 
Figure A2a presents 12-month size-and-book-to-market-adjusted returns from taking long positions in stocks with 𝑃𝑃𝑃𝑃
෢> 0.5 and short positions in stocks with 
𝑃𝑃𝑃𝑃
෢≤ 0.5 using random forests. Figure A2b presents 12-month size-and-book-to-market-adjusted returns from taking long positions in stocks with 𝑃𝑃𝑃𝑃
෢≥ 0.6 and 
short positions in stocks with 𝑃𝑃𝑃𝑃
෢≤ 0.4 using random forests. Figure A2c shows 12-month size-and-book-to-market-adjusted returns from taking long positions 
in stocks with 𝑃𝑃𝑃𝑃
෢> 0.5 and short positions in stocks with 𝑃𝑃𝑃𝑃
෢≤ 0.5 using stochastic gradient boosting. Figure A2d shows 12-month size-and-book-to-market-
adjusted returns from taking long positions in stocks with 𝑃𝑃𝑃𝑃
෢≥ 0.6 and short positions in stocks with 𝑃𝑃𝑃𝑃
෢≤ 0.4 using stochastic gradient boosting. The size-and-
book-to-market-adjusted returns are calculated by subtracting the value-weighted buy-and-hold returns on the size-and-book-to-market-matched 5x5 portfolio 
from the buy-and-hold sample firm returns. When constructing the size-and-book-to-market portfolios, all the NYSE firms are ranked according to their market 
value of equity to be placed into quintiles and also according to their book-to-market to be placed into quintiles. Then, NASDAQ and AMEX firms are put into 
one of the 25 intersecting portfolios based on their market value of equity and book-to-market. The annual return for each of the 25 intersecting portfolios is 
calculated by taking the market value-weighted average of the annual returns of the constituent firms. 
 
 
 
Figure A2a Random forests with 𝑃𝑃𝑃𝑃
෢ > 0.5 and <0.5 
 
Figure A2b Random forests with 𝑃𝑃𝑃𝑃
෢ > 0.6 and <0.4 
 
Electronic copy available at: https://ssrn.com/abstract=3741015

## Page 91

28 
 
 
 
Figure A2c Stochastic gradient boosting with 𝑃𝑃𝑃𝑃
෢ > 0.5 and <0.5 
 
Figure A2d Stochastic gradient boosting with 𝑃𝑃𝑃𝑃
෢ > 0.6 and <0.4 
 
 
 
Electronic copy available at: https://ssrn.com/abstract=3741015
